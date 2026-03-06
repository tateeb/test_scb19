from odoo import http
from odoo.http import request
import logging
from odoo.exceptions import ValidationError
import json

_logger = logging.getLogger(__name__)


class SCBController(http.Controller):

    @http.route('/payment/scb/webhook', type='json', auth='public', methods=['POST','GET'], csrf=False)
    def scb_webhook(self, **kwargs):
        """ รับข้อมูล Callback จาก SCB """
        data = request.get_json_data()
        _logger.info(">>> SCB Webhook Received: %s", data)

        # 1. ดึงข้อมูลจากก้อน Data (SCB มักส่งครอบด้วย data: {...})
        payload = data.get('data', {}) if 'data' in data else data
        ref1 = payload.get("billPaymentRef1") or data.get("reference1")

        if not ref1:
            return {"status": {"code": "400", "description": "Missing Reference"}}

        # 2. ค้นหา Transaction (แนะนำให้หาจากฟิลด์ที่เราเก็บ ref1 ไว้ตอนเจน QR)
        tx = request.env['payment.transaction'].sudo().search([
            ('reference', '=', ref1)  # หรือ 'scb_ref1' ตามที่คุณตั้งชื่อฟิลด์ไว้
        ], limit=1)

        if not tx:
            _logger.warning(">>> SCB: Transaction not found for Ref: %s", ref1)
            return {"status": {"code": "404", "description": "Transaction not found"}}

        # 3. ตรวจสอบว่าสำเร็จจริงไหม (SCB ส่ง status.code == 1000)
        # หมายเหตุ: status มักจะอยู่นอกก้อน data
        scb_status = data.get('status', {})
        if scb_status.get('code') != 1000:
            _logger.warning(">>> SCB Webhook: Transaction not successful. Status: %s", scb_status)
            return {"status": {"code": "1000", "description": "Acknowledged (Not Success)"}}

        # 4. เรียกใช้ Model Logic เพื่อตรวจสอบจำนวนเงินและบันทึก Done
        try:
            # ส่งเฉพาะก้อน payload (data) ไปเช็ค
            if tx._handle_notification_data('scb',payload):
                return {"status": {"code": "1000", "description": "Success"}}
            else:
                return {"status": {"code": "400", "description": "Validation Failed"}}
        except Exception as e:
            _logger.error(">>> SCB Webhook Error: %s", str(e))
            return {"status": {"code": "500", "description": "Internal Server Error"}}

    @http.route('/payment/scb/status/<int:tx_id>', type='json', auth='public', methods=['POST','GET'], csrf=False)
    def scb_get_status(self, tx_id, **kwargs):
        """ ตรวจสอบสถานะ Transaction เพื่อให้ Javascript Polling นำไปใช้ Redirect """

        tx = request.env['payment.transaction'].sudo().browse(tx_id)

        if not tx.exists():
            _logger.warning(">>> SCB Status Check: Transaction ID %s not found", tx_id)
            return {'state': 'error', 'message': 'not_found'}

        # ส่วนเสริม: ถ้าสถานะยังไม่สำเร็จ ให้ลองสะกิด Inquiry API ของธนาคารอีกครั้ง (ถ้าจำเป็น)
        if tx.state in ['draft', 'pending']:
            try:
                # ตรวจสอบว่ามีฟังก์ชัน Inquiry ใน Model หรือยัง (อ้างอิงจากบทสนทนาก่อนหน้า)
                if hasattr(tx, '_scb_inquiry_status'):
                    tx._scb_inquiry_status()
            except Exception as e:
                _logger.error(">>> SCB Inquiry during polling failed: %s", str(e))

        # ส่งสถานะกลับไปในรูปแบบ JSON Object
        return {
            'state': tx.state,
            'reference': tx.reference,
            'is_done': tx.state in ['done', 'authorized'],  # ถ้าเป็น True หน้าจอจะ Redirect ทันที
            'is_cancel': tx.state == 'cancel',
            'landing_route': tx.landing_route,  # เส้นทางที่จะให้ Redirect ไปหลังจ่ายเสร็จ
        }

    @http.route('/payment/scb/display_qr/<int:tx_id>', type='http', auth='public', website=True)
    def scb_qr_page(self, tx_id, **kw):

        tx = request.env['payment.transaction'].sudo().browse(tx_id)

        # ค้นหา Transaction
        # 1. กรณีไม่พบ Transaction
        if not tx.exists():
            return request.not_found()

        # เตรียมข้อมูลพื้นฐานที่ต้องใช้ในทุกเงื่อนไข
        merchant_display = tx.provider_id.scb_merchant_id or tx.company_id.name
        values = {
            'tx': tx,
            'amount': tx.amount,
            'currency': tx.currency_id,
            'reference': tx.reference,
            'merchant_name': merchant_display,
            'qr_code': False,
            'error_msg': False,
        }

        # 2. กรณีถูกยกเลิก (เช่น มีการสร้าง Transaction ใหม่ทับ)
        if tx.state == 'cancel':
            values['error_msg'] = "QR Code นี้ถูกยกเลิกแล้วเนื่องจากมีการสร้างรายการใหม่"
            return request.render('payment_scb.scb_qr_checkout_page', values)

        # 3. กรณีจ่ายสำเร็จแล้ว หรือไม่มีรูป QR
        if tx.state != 'pending' or not tx.scb_qr_image:
            values['error_msg'] = "รายการนี้ถูกประมวลผลแล้วหรือหมดอายุ กรุณาตรวจสอบสถานะคำสั่งซื้อ"
            return request.render('payment_scb.scb_qr_checkout_page', values)

        # 4. กรณีปกติ (แสดง QR)
        try:
            qr_image = tx.scb_qr_image
            values['qr_code'] = qr_image.decode('utf-8') if isinstance(qr_image, bytes) else qr_image
        except Exception:
            values['error_msg'] = "ไม่สามารถแสดงผล QR Code ได้ กรุณาลองใหม่อีกครั้ง"

        return request.render('payment_scb.scb_qr_checkout_page', values)

    # @http.route('/payment/scb/confirm', type='http', auth='public', methods=['POST'], website=True)
    # def scb_confirm_payment(self, tx_id, method, **kwargs):
    #     """รับค่าจากฟอร์มเลือกวิธีชำระเงิน"""
    #     tx_sudo = request.env['payment.transaction'].sudo().browse(int(tx_id))
    #
    #     if method == 'qrcode':
    #         # เรียกฟังก์ชันที่คุณเขียนไว้ใน Model
    #         success = tx_sudo._scb_generate_qr_action()
    #         if success:
    #             return request.render('payment_scb.scb_qr_display', {'tx': tx_sudo})
    #
    #     elif method == 'creditcard':
    #         # ตัวอย่างการรองรับวิธีอื่นในอนาคต
    #         return request.redirect('/payment/scb/credit_card_flow/%s' % tx_id)
    #
    #     return request.redirect('/shop/payment')