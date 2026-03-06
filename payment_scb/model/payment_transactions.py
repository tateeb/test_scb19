# -*- coding: utf-8 -*-
import datetime
import uuid
import qrcode
import base64
import requests
import logging
import re
from io import BytesIO

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError
from odoo.http import request
from PIL import Image
_logger = logging.getLogger(__name__)
from odoo.tools.misc import file_path
_logger.info("!!!!!!!!!! SCB TRANSACTION FILE LOADED !!!!!!!!!!")

class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    # ฟิลด์สำหรับ SCB
    scb_txn_ref = fields.Char(string='SCB Transaction Ref')
    scb_qr_image = fields.Binary(string="SCB QR Image", attachment=True)
    scb_qr_raw_data = fields.Text(string="SCB QR Raw Data", readonly=True)
    scb_reference1 = fields.Char(string="SCB Reference1")

    # ตาราง Log สำหรับตรวจสอบ API (ต้องมี model payment.transaction.log ใน module)
    scb_log_ids = fields.One2many('payment.transaction.log', 'transaction_id', string='SCB API Logs')
    scb_payment_log_ids = fields.One2many(
        'scb.payment.log',
        'transaction_id',
        string='SCB Logs'
    )

    def _add_scb_log(self, message, log_type='info', payload=None):
        """ บันทึก Log การทำงานของ API """
        self.env['payment.transaction.log'].sudo().create({
            'transaction_id': self.id,
            'log_type': log_type,
            'message': message,
            'payload': str(payload) if payload else False,
        })
        _logger.info(f"SCB LOG [{self.reference}]: {message}")

    # ==============================
    # SCB Helpers (Revised for SCB)
    # ==============================
    def _set_pending(self, state_message=None):
        """เปลี่ยนสถานะเป็น Pending และแยก SO ออกจาก Cart"""
        for tx in self:
            if tx.state != 'draft':
                continue

            tx.write({
                'state': 'pending',
                'state_message': state_message or 'ระบบกำลังรอรับชำระเงินผ่าน SCB QR Code...',
            })

            # แยก Sale Order ออกจาก Cart ทันที
            for so in tx.sale_order_ids.filtered(lambda s: s.state == 'draft'):
                so.action_quotation_sent()
                _logger.info(
                    ">>> SCB: SO %s moved to SENT after QR generation (TX: %s)",
                    so.name, tx.reference
                )

            _logger.info(">>> SCB: Transaction %s changed to PENDING", tx.reference)

    def _set_done(self):
        self.ensure_one()
        if self.state != 'done':
            # 1. จัดการ Method Line ก่อน
            if hasattr(self, '_get_or_setup_method_line'):
                self._get_or_setup_method_line()

            # 2. บันทึกสถานะ DONE
            self.write({
                'state': 'done',
                'state_message': 'Payment Success',
                'last_state_change': fields.Datetime.now(),
            })

            # 3. ยืนยัน Sale Order
            self._confirm_so()

            if hasattr(self, '_post_process'):
                try:
                    self._post_process()
                    self.env.cr.flush()
                except Exception as e:
                    _logger.warning(">>> SCB Post Process Warning: %s", str(e))

                # 5️⃣ ตรวจสอบว่ามี Invoice หรือยัง (รองรับทั้ง 2 กรณี)
            self._ensure_invoice_created()
            # 5. ทำการตัดจ่าย (Reconcile)
            self._reconcile_after_done()

    def _set_canceled(self, state_message=None):
        """ เปลี่ยนสถานะเป็น 'Canceled' """
        self.ensure_one()
        if self.state not in ['cancel', 'done']:
            self.write({
                'state': 'cancel',
                'state_message': state_message or 'รายการนี้ถูกยกเลิกแล้ว',
            })
            for so in self.sale_order_ids:

                so.message_post(body=_(
                    "การชำระเงิน (Ref: %s) หมดเวลาหรือถูกยกเลิก "
                    "ใบเสนอราคาถูกแยกออกจาก Cart แล้ว"
                ) % self.reference)

            _logger.info(">>> SCB: Transaction %s canceled. Sale Order remains active.", self.reference)

    def _confirm_so(self):
        for tx in self:
            # ดึงเฉพาะ SO ที่ยังไม่ได้ Confirm
            target_sos = tx.sale_order_ids.sudo().filtered(lambda s: s.state in ['draft', 'sent'])
            for so in target_sos:
                try:
                    # สร้างจุดเซฟ (Savepoint)
                    # ถ้าข้างในนี้พัง มันจะ Rollback กลับมาแค่ตรงนี้ ไม่ทำลายทั้ง Transaction
                    with self.env.cr.savepoint():
                        if so.state in ['draft', 'sent']:
                            _logger.info(">>> SCB: Attempting to Confirm SO %s", so.name)
                            so.action_confirm()
                except Exception as e:
                    # ถ้า Confirm พัง (เช่น duplicate follower) ให้ Log ไว้แต่ไม่หยุดการทำงาน
                    _logger.error(">>> SCB Confirm SO Error (Recovered): %s", str(e))
                    # บันทึกลงใน Log ของ Transaction ด้วยเพื่อให้เรารู้ว่าต้องมาตามกดเอง
                    # tx._message_log(body=f"Confirm SO {so.name} failed but payment is kept: {str(e)}")
                    tx.message_post(body=f"Confirm SO {so.name} failed but payment is kept: {str(e)}")

    def _reconcile_after_done(self):
        """
        ระบบบันทึกบัญชีอัตโนมัติ (Reconcile)
        เน้นดึง Invoice จาก Sale Order และจัดการ Payment ให้ขึ้นสถานะ PAID
        """
        if self.provider_code not in ['scb']:
            return

        _logger.info(">>> %s: Starting Auto Reconcile for %s", self.provider_code.upper(), self.reference)

        if self.operation != 'refund':
            # 1. บังคับบันทึกข้อมูลและล้าง Cache เพื่อให้เห็น Invoice ล่าสุด
            self.env.cr.flush()
            self.invalidate_recordset(['invoice_ids'])
            if self.sale_order_ids:
                self.sale_order_ids.invalidate_recordset(['invoice_ids'])

            # 2. รวบรวม Invoice ที่เกี่ยวข้อง (ทั้งที่ติดมากับ TX และที่อยู่ใน SO)
            target_invoices = self.invoice_ids.sudo().filtered(lambda inv: inv.state != 'cancel')
            if self.sale_order_ids:
                so_invoices = self.sale_order_ids.sudo().mapped('invoice_ids').filtered(
                    lambda inv: inv.state != 'cancel')
                target_invoices |= so_invoices

            if not target_invoices:
                _logger.warning(">>> %s: No invoices found to reconcile for %s. Check Invoicing Policy!",
                                self.provider_code.upper(), self.reference)
                return

            _logger.info(">>> %s: Found %s invoice(s) for Reconcile", self.provider_code.upper(), len(target_invoices))

            for invoice in target_invoices:
                invoice = invoice.sudo()

                # --- ส่วนที่ 1: เชื่อมโยง Transaction กับ Invoice (นำกลับมา) ---
                if self not in invoice.transaction_ids:
                    invoice.write({'transaction_ids': [(4, self.id)]})
                    _logger.info(">>> %s: Linked TX to Invoice %s", self.provider_code.upper(), invoice.name)

                # --- ส่วนที่ 2: ยืนยัน Invoice หากยังเป็น Draft (นำกลับมา) ---
                if invoice.state == 'draft':
                    try:
                        invoice.action_post()
                        _logger.info(">>> %s: Automated Post for Invoice %s", self.provider_code.upper(), invoice.name)
                    except Exception as e:
                        _logger.error(">>> %s: Could not post invoice %s: %s", self.provider_code.upper(), invoice.name,
                                      str(e))
                        continue

                # --- ส่วนที่ 3: ตรวจสอบยอดและสร้าง Payment (เรียกฟังก์ชันเดิมที่คุณเขียนไว้) ---
                if invoice.state == 'posted' and invoice.payment_state not in ['paid', 'in_payment']:
                    if invoice.amount_residual > 0:
                        _logger.info(">>> %s: Initiating payment for %s (Residual: %s)",
                                     self.provider_code.upper(), invoice.name, invoice.amount_residual)
                        # ฟังก์ชันนี้จะไปจัดการเรื่อง Journal และ Method Line ตามที่คุณเขียนไว้ข้างล่าง
                        self._create_payment_for_invoice(invoice)
                    else:
                        _logger.info(">>> %s: Invoice %s has no residual amount. Skipping payment.",
                                     self.provider_code.upper(), invoice.name)

        # --- ส่วนที่ 4: รองรับ Refund (นำกลับมา) ---
        elif self.operation == 'refund' and self.state == 'done':
            _logger.info(">>> %s: Processing Refund Reconcile", self.provider_code.upper())

    def _do_scb_payment_reconcile(self, invoice):
        """ ทำการ Reconcile ให้ Invoice ขึ้นสถานะ Paid """
        if invoice.state == 'draft':
            invoice.action_post()

        payment = self.payment_id or self.env['account.payment'].sudo().search([
            ('payment_transaction_id', '=', self.id)
        ], limit=1)

        if payment:
            # ผูก Payment Method ของ SCB
            if hasattr(self, '_get_or_setup_method_line'):
                method_line = self._get_or_setup_method_line()
                if method_line:
                    payment.payment_method_line_id = method_line.id

            if payment.state == 'draft':
                payment.action_post()

            # จับคู่ยอดเงิน
            if invoice.payment_state not in ['paid', 'in_payment']:
                lines = (payment.move_id.line_ids + invoice.line_ids).filtered(
                    lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
                )
                if len(lines) > 1:
                    lines.reconcile()
                    _logger.info(">>> SCB: Invoice %s is now PAID", invoice.name)

    # ==============================
    # Handle SCB Webhook / Inquiry Result
    # ==============================
    def _handle_scb_webhook(self, payload):
        """ รับ Data จาก Inquiry หรือ Webhook มาประมวลผลสถานะ """
        self.ensure_one()
        self._add_scb_log("Processing SCB Data", 'info', payload)

        if self.state in ('done', 'cancel'):
            return True

        # SCB มักจะส่ง transRef มาให้ถ้าชำระสำเร็จ
        txn_ref = payload.get('transRef') or payload.get('transactionId')

        # บันทึกรหัสอ้างอิงจากธนาคาร
        if txn_ref:
            self.write({'scb_txn_ref': txn_ref})

        # SCB Logic: ถ้ามี txn_ref และยอดเงินถูกต้อง (หรือมี status SUCCESS)
        # หมายเหตุ: ปรับเงื่อนไขตาม Response ของธนาคารที่ได้รับจริง
        if txn_ref:
            self._add_scb_log(f"Payment Success (Ref: {txn_ref})", 'info')
            self._set_done()
        else:
            _logger.warning(">>> SCB Webhook: No transaction reference found for %s", self.reference)

        return True

    # =========================================================================
    # 1. Processing Values (Odoo 18 Override)
    # =========================================================================
    def _get_specific_processing_values(self, processing_values):
        """ Override Odoo 18 to process SCB-specific values. """
        res = super()._get_specific_processing_values(processing_values)

        if self.provider_code != 'scb':
            return res

        _logger.info(">>> SCB: Starting QR Creation for Transaction: %s", self.reference)

        # เรียกฟังก์ชันสร้าง QR ของ SCB
        self._scb_create_payment()
        rendering_url = f'/payment/scb/display_qr/{self.id}'

        return {
            'api_url': rendering_url,
            'redirect_form_html': f'<form action="{rendering_url}" method="get"></form>',
        }

    # =========================================================================
    # 2. Adjust Reference (SCB: Alpha-Numeric Only)
    # =========================================================================
    def _scb_safe_reference(self):
        self.ensure_one()
        # SCB กฎเหล็ก: ห้ามมีขีด (-) ห้ามมีช่องว่าง ใช้ได้แค่ A-Z และ 0-9 เท่านั้น
        return re.sub(r'[^A-Z0-9]', '', self.reference.upper())[:20]

    # =========================================================================
    # 3. Create Payment & Call SCB API
    # =========================================================================
    def _scb_create_payment(self):
        self.ensure_one()

        # if self.sale_order_ids:
        #     # 1. ค้นหารายการเก่าที่ยังไม่สำเร็จของ SO เดียวกัน
        #     old_txs = self.search([
        #
        #         ('sale_order_ids', 'in', self.sale_order_ids.ids),
        #         ('id', '!=', self.id),
        #         ('state', 'in', ['draft', 'pending']),
        #         ('provider_code', '=', 'scb')
        #     ])
        #
        #     # 2. ยกเลิกรายการเก่าทิ้งพร้อมบันทึก Message
        #     if old_txs:
        #         _logger.info(">>> SCB: Cancelling %s old transactions for SO %s",
        #                      len(old_txs), self.sale_order_ids.mapped('name'))
        #         for tx in old_txs:
        #             # ใช้คำสั่งยกเลิกมาตรฐานของ Odoo
        #             tx._set_canceled(_("Cancelled due to a new payment request being created (%s)") % self.reference)

        group_key = self._get_so_group_key()
        if group_key and self.sale_order_ids:
            # Find other unfinished transactions for this SO.
            old_txs = self.search([
                ('sale_order_ids', 'in', self.sale_order_ids.ids),
                ('id', '!=', self.id),
                ('state', 'in', ['draft', 'pending']),
                ('provider_code', '=', 'scb')
            ])
            if old_txs:
                _logger.info(">>> Clearing %s old transactions for SO: %s", len(old_txs), group_key)
                for tx in old_txs:
                    # Immediately cancel the previous transaction.
                    tx._set_canceled(_("Cancelled due to a new payment request being created (%s)") % self.reference)

                # Force commit the 'Cancel' state to the database.
                # self.env.cr.commit()

        provider = self.provider_id
        self._add_scb_log("Starting QR generation process", 'info')
        token = provider._scb_get_access_token()  # ใช้ฟังก์ชัน OAuth ที่เราเขียนไว้ก่อนหน้า

        # เตรียมข้อมูลสำหรับ Payload
        safe_ref = self._scb_safe_reference()
        qr_display_name = provider.scb_merchant_id or self.company_id.name

        # SCB Payload Structure
        payload = {
            "qrType": "PP",
            "ppType": "BILLERID",
            "ppId": provider.scb_biller_id,
            "amount": "{:.2f}".format(self.amount),
            "ref1": safe_ref,
            "ref2": "ORDER",
            "ref3": getattr(provider, 'scb_ref3_prefix', 'ODOO') or "ODOO"
        }

        headers = {
            "accept-language": "EN",
            "Content-Type": "application/json",
            "authorization": f"Bearer {token}",
            "requestUId": str(uuid.uuid4()),
            "resourceOwnerId": provider.scb_api_key
        }

        # URL สำหรับสร้าง QR (v1/qr/qrcode)
        endpoint = f"{provider.scb_base_url}/v1/payment/qrcode/create"
        self._add_scb_log(f"Sending Request to SCB QR API: {endpoint}", 'info', payload)

        try:
            _logger.info(">>> SCB Request Payload: %s", payload)
            response = requests.post(endpoint, json=payload, headers=headers, timeout=20)
            res_data = response.json()
            self._add_scb_log("Received Response from SCB QR API", 'info', res_data)

            if response.status_code != 200 or res_data.get('status', {}).get('code') != 1000:
                error_msg = res_data.get('status', {}).get('description', 'Unknown Error')
                _logger.error(">>> SCB API Error: %s", error_msg)
                self._add_scb_log(f"SCB API Error: {error_msg}", 'error', res_data)
                raise UserError(f"SCB API Error: {error_msg}")

            # ดึงข้อมูล Raw QR Data (มักจะอยู่ใน data -> qrRawData)
            qr_raw_data = res_data.get('data', {}).get('qrRawData')
            self._add_scb_log("Generating QR Image with Logo", 'info', {"qrRawData": qr_raw_data})
            if not qr_raw_data:
                raise UserError("QR Data not found in SCB response.")

            # ==========================================
            # 4. สร้างรูปภาพ QR Code (รวมร่างกับ Template)
            # ==========================================
            qr = qrcode.QRCode(version=1, box_size=10, border=2)
            qr.add_data(qr_raw_data)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white").convert('RGB')

            # โหลด Template สีม่วงของ SCB
            logo_path = file_path('payment_scb/static/thaiqr_assets/logo.png')

            try:
                if logo_path:
                    logo = Image.open(logo_path).convert("RGBA")
                    qr_w, qr_h = qr_img.size

                    # ปรับขนาดโลโก้ประมาณ 15-20% ของ QR
                    logo_scale = 0.18
                    logo_width = int(qr_w * logo_scale)
                    w_percent = (logo_width / float(logo.size[0]))
                    logo_height = int((float(logo.size[1]) * float(w_percent)))
                    logo = logo.resize((logo_width, logo_height), Image.Resampling.LANCZOS)

                    # วางโลโก้กึ่งกลาง
                    logo_pos = ((qr_w - logo_width) // 2, (qr_h - logo_height) // 2)
                    qr_img.paste(logo, logo_pos, logo)
            except Exception as e:
                _logger.error("Logo processing error: %s", str(e))

            # แปลงเป็น Base64
            buffered = BytesIO()
            qr_img.save(buffered, format="PNG")
            qr_image_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')

            # บันทึกลง Transaction
            self.write({
                "scb_qr_image": qr_image_base64,
                "scb_reference1": safe_ref,
            })
            self._add_scb_log("QR Code successfully generated and saved to transaction", 'info')
            self._set_pending()
            # self.env.cr.commit()

        except Exception as e:
            self._add_scb_log(f"Critical Error in _scb_create_payment: {str(e)}", 'error')
            _logger.error(">>> SCB Connection Error: %s", str(e))
            raise UserError("ไม่สามารถติดต่อธนาคาร SCB ได้ในขณะนี้")

    def _scb_inquiry_status(self):
        self.ensure_one()
        provider = self.provider_id

        self._add_scb_log("=== SCB Inquiry START ===", 'info')
        _logger.info("SCB Inquiry START | TX: %s", self.reference)

        # -------------------------------------------------
        # 1️ Get Access Token
        # -------------------------------------------------
        token = provider._scb_get_access_token()
        if not token:
            msg = "SCB Inquiry: Failed to obtain Access Token"
            _logger.error(msg)
            self._add_scb_log(msg, 'error')
            return False

        # -------------------------------------------------
        # 2️ Prepare URL & Headers
        # -------------------------------------------------
        url = provider.scb_api_url_qr_inquiry

        headers = {
            "accept-language": "EN",
            "Content-Type": "application/json",
            "authorization": f"Bearer {token}",
            "requestUId": str(uuid.uuid4()),
            "resourceOwnerId": provider.scb_api_key
        }

        txn_date = (
            self.create_date.strftime('%Y-%m-%d')
            if self.create_date
            else fields.Date.today().strftime('%Y-%m-%d')
        )

        params = {
            "eventCode": "00300100",
            "billerId": provider.scb_biller_id,
            "reference1": self.scb_reference1,
            "reference2": "ORDER",
            "reference3": getattr(provider, 'scb_ref3_prefix', 'ODOO') or "ODOO",
            "transactionDate": txn_date,
        }

        self._add_scb_log(
            f"Sending Inquiry Request → {url}",
            'request',
            {
                "headers": headers,
                "params": params
            }
        )

        _logger.info(
            "SCB Inquiry REQUEST | TX: %s | Ref1: %s | Date: %s",
            self.reference,
            self.scb_reference1,
            txn_date
        )

        # -------------------------------------------------
        # 3️ Send Request
        # -------------------------------------------------
        try:
            res = requests.get(url, headers=headers, params=params, timeout=20)

            http_status = res.status_code
            response_text = res.text

            try:
                result = res.json()
            except Exception:
                result = {"raw_response": response_text}

            self._add_scb_log(
                f"SCB Inquiry RESPONSE (HTTP {http_status})",
                'response',
                result
            )

            _logger.info(
                "SCB Inquiry RESPONSE | TX: %s | HTTP: %s",
                self.reference,
                http_status
            )

            # -------------------------------------------------
            # 4️ Process Result
            # -------------------------------------------------
            if http_status != 200:
                msg = f"SCB Inquiry HTTP Error: {http_status}"
                _logger.error(msg)
                self._add_scb_log(msg, 'error')
                return False

            status_node = result.get('status', {})
            status_code = status_node.get('code')
            status_desc = status_node.get('description', '')

            if status_code != 1000:
                msg = f"SCB API Status Error: {status_code} - {status_desc}"
                _logger.warning(msg)
                self._add_scb_log(msg, 'warning', result)
                return False

            # -------------------------------------------------
            # 5️ Extract Data
            # -------------------------------------------------
            data = result.get('data')

            if isinstance(data, list) and data:
                data = data[0]

            if data and (data.get('transRef') or data.get('transactionId')):
                txn_id = data.get('transRef') or data.get('transactionId')

                msg = f"Payment FOUND via Inquiry | Bank Ref: {txn_id}"
                _logger.info(msg)
                self._add_scb_log(msg, 'info', data)

                # ใช้มาตรฐาน Odoo
                self._handle_notification_data('scb',data)

                self._add_scb_log("=== SCB Inquiry SUCCESS ===", 'info')
                return True

            else:
                msg = "Inquiry Result: Payment not found yet (Pending)"
                _logger.info("SCB Inquiry PENDING | TX: %s", self.reference)
                self._add_scb_log(msg, 'info')
                return False

        except requests.exceptions.Timeout:
            msg = "SCB Inquiry Timeout Error"
            _logger.error(msg)
            self._add_scb_log(msg, 'error')
            return False

        except requests.exceptions.ConnectionError:
            msg = "SCB Inquiry Connection Error"
            _logger.error(msg)
            self._add_scb_log(msg, 'error')
            return False

        except Exception as e:
            msg = f"SCB Inquiry Unexpected Error: {str(e)}"
            _logger.exception(msg)
            self._add_scb_log(msg, 'error')
            return False

    def _handle_notification_data(self,provider_code, notification_data):
        """ ฟังก์ชันมาตรฐานของ Odoo สำหรับจัดการข้อมูลที่ได้รับมา """
        if self.state == 'done':
            return True

        if provider_code != 'scb':
            return super()._handle_notification_data(provider_code, notification_data)

        # ดึงรหัสอ้างอิงธนาคาร
        txn_id = notification_data.get('transRef') or notification_data.get('transactionId')

        if txn_id:
            self.provider_reference = txn_id
            self.scb_txn_ref = txn_id
            self._create_scb_audit_log(notification_data)
            # สั่งให้สถานะเป็น Done
            self._set_done()
            # for so in self.sale_order_ids.filtered(lambda x: x.state in ['draft', 'sent']):
            #     so.action_confirm()
            #     _logger.info(">>> SCB: Sale Order %s confirmed automatically after payment", so.name)
            # บันทึก Log
            self._add_scb_log(f"Payment confirmed via Inquiry/Webhook. ID: {txn_id}", 'info')

    def _create_payment_for_invoice(self, invoice):
        """ สร้างและตัดจ่ายเงินให้กับ Invoice อัตโนมัติ (รองรับ SCB) """
        self.ensure_one()
        try:
            # 0. ตรวจสอบก่อนว่า Invoice พร้อมจ่ายหรือไม่
            if invoice.state != 'posted' or invoice.payment_state in ['paid', 'in_payment']:
                _logger.info(">>> Payment: Invoice %s is already paid or not posted. Skipping.", invoice.name)
                return False

            # 1. ตรวจสอบ Draft Payment เดิมเพื่อกันการสร้างซ้ำ
            existing_payment = self.env['account.payment'].sudo().search([
                ('ref', 'ilike', self.reference),
                ('state', '=', 'draft'),
                ('partner_id', '=', invoice.partner_id.id),
                ('is_reconciled', '=', False)
            ], limit=1)

            if existing_payment:
                payment = existing_payment
                _logger.info(">>> Payment: Found existing DRAFT payment %s, confirming...", payment.name)
            else:
                # 2. ค้นหาหรือ Setup Payment Method Line
                method_line = self._get_or_setup_method_line()
                if not method_line:
                    _logger.error(">>> Payment: Could not find/setup payment method line for %s", self.provider_code)
                    return False

                # 3. สร้าง Payment ใหม่
                payment_vals = {
                    'payment_type': 'inbound',
                    'partner_type': 'customer',
                    'partner_id': invoice.partner_id.id,
                    'amount': invoice.amount_residual,
                    'journal_id': method_line.journal_id.id,
                    'payment_method_line_id': method_line.id,
                    'ref': f'{self.provider_code.upper()} QR: {self.reference}',
                    'payment_token_id': self.token_id.id if hasattr(self, 'token_id') else False,
                }
                payment = self.env['account.payment'].sudo().create(payment_vals)
                _logger.info(">>> Payment: Created new payment %s for %s", payment.name, self.reference)

            # 4. ยืนยัน Payment
            if payment.state == 'draft':
                payment.action_post()

            # 5. ทำการ Reconcile (เพื่อให้ขึ้นแถบ PAID)
            # ดึง Account Line ที่เป็น Receivable
            lines = (payment.move_id.line_ids + invoice.line_ids).filtered(
                lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
            )

            if len(lines) > 1:
                lines.reconcile()
                _logger.info(">>> Payment: SUCCESS! Invoice %s is now PAID", invoice.name)
                return True

            return False

        except Exception as e:
            _logger.error(">>> Payment Error for %s: %s", self.reference, str(e))
            return False

    def _get_or_setup_method_line(self):
        """ ค้นหาหรือสร้างแถบวิธีการชำระเงินใน Journal """
        self.ensure_one()
        provider = self.provider_id

        # ค้นหา Journal (ลำดับความสำคัญ: จาก Provider > จากคลังข้อมูล Bank)
        journal = provider.journal_id or self.env['account.journal'].search([
            ('type', '=', 'bank'),
            ('company_id', '=', self.company_id.id)
        ], limit=1)

        if not journal:
            return False

        # ค้นหา Method Line ที่เชื่อมกับ Provider นี้
        method_line = journal.inbound_payment_method_line_ids.filtered(
            lambda l: l.payment_provider_id == provider or l.name == f'{provider.code.upper()} QR Payment'
        )

        if not method_line:
            # ใช้ code 'electronic' สำหรับ Odoo 17/18
            payment_method = self.env['account.payment.method'].search([
                ('code', '=', 'electronic'),
                ('payment_type', '=', 'inbound')
            ], limit=1) or self.env['account.payment.method'].search([
                ('code', '=', 'manual'),
                ('payment_type', '=', 'inbound')
            ], limit=1)

            if payment_method:
                method_line = self.env['account.payment.method.line'].sudo().create({
                    'name': f'{provider.code.upper()} QR Payment',
                    'journal_id': journal.id,
                    'payment_method_id': payment_method.id,
                    'payment_provider_id': provider.id,
                })

        return method_line[:1]

    def _ensure_invoice_created(self):
        """
        ตรวจสอบก่อนว่าใน Settings ได้เปิด 'Automatic Invoice' ไว้หรือไม่
        1. ถ้าเปิด (True): ระบบจะพยายามสร้าง Invoice ให้ทันทีหากพร้อม (To Invoice)
        2. ถ้าไม่เปิด (False): จะไม่สร้าง Invoice อัตโนมัติ เพื่อรอให้ผู้ใช้ไปกดสร้างเอง (Manual)
        """
        self.ensure_one()
        if not self.sale_order_ids:
            return

        # ดึงค่า Config จากหน้า Settings (Automatic Invoice)
        # ค่าที่ได้จะเป็น 'True' (string) หรือ False (None)
        auto_invoice_setting = self.env['ir.config_parameter'].sudo().get_param('sale.automatic_invoice')

        _logger.info(">>> SCB: Automatic Invoice Setting is %s", auto_invoice_setting)

        for so in self.sale_order_ids:
            # 1. ค้นหา Invoice ที่มีอยู่แล้ว (เผื่อ Odoo สร้างให้ไปแล้วหรือมีคนกดไว้ก่อน)
            existing_invoices = so.invoice_ids.filtered(lambda inv: inv.state != 'cancel')

            if existing_invoices:
                _logger.info(">>> SCB: Invoice already exists for %s. Skipping creation.", so.name)
                continue

            # 2. ถ้ายังไม่มี Invoice และตรวจสอบว่า "เปิดการตั้งค่า Automation" หรือไม่
            if auto_invoice_setting:
                # ตรวจสอบว่า SO พร้อมออกบิลตาม Invoicing Policy หรือไม่ (to invoice)
                if so.invoice_status == 'to invoice':
                    try:
                        _logger.info(">>> SCB: System Settings 'Auto-Invoice' is ON. Creating invoice for %s", so.name)
                        # สั่งสร้างและ Post Invoice อัตโนมัติ
                        new_invoice = so.sudo()._create_invoices(final=True)
                        if new_invoice:
                            new_invoice.action_post()
                    except Exception as e:
                        _logger.error(">>> SCB: Auto-invoice creation failed: %s", str(e))
                else:
                    _logger.info(">>> SCB: Auto-Invoice is ON but SO %s is NOT READY (Check Policy).", so.name)
            else:
                # 3. ถ้าไม่ได้ติ๊กถูกหน้า Automatic Invoice
                _logger.info(">>> SCB: Automatic Invoice Setting is OFF. Waiting for manual invoice creation for %s.",
                             so.name)

    def _get_so_group_key(self):
        """ Use primary SO as key (e.g., SO1, SO1-1 → SO1). """
        self.ensure_one()
        if not self.sale_order_ids:
            return False
        # ดึงชื่อ SO แรกและตัดเอาแค่ส่วนหน้าเครื่องหมาย '-'
        return self.sale_order_ids[0].name.split('-')[0]

    @api.model
    def _cron_check_scb_payments(self):
        """
        ส่วนที่ 1: ตรวจสอบการชำระเงิน (Run ทุก 1-2 นาที)
        เน้นรายการที่เพิ่งสร้างและยังเป็น Pending อยู่
        """
        # เช็ครายการที่สร้างภายใน 24 ชม. และยังไม่สำเร็จ
        now = fields.Datetime.now()
        start_time = now - datetime.timedelta(days=1)

        pending_txs = self.search([
            ('provider_code', '=', 'scb'),
            ('state', '=', 'pending'),
            ('create_date', '>', start_time)
        ])

        _logger.info(">>> SCB Cron Inquiry: Checking %s active payment(s).", len(pending_txs))

        for tx in pending_txs:
            try:
                # เรียก Inquiry API โดยตรง
                tx._scb_inquiry_status()
            except Exception as e:
                _logger.error(">>> SCB Cron Inquiry Error for %s: %s", tx.reference, str(e))

    @api.model
    def _cron_cleanup_expired_scb_payments(self):
        """
        ส่วนที่ 2: จัดการรายการหมดอายุ (Run ทุก 10-15 นาที)
        ยกเลิกรายการที่ค้างนานเกินกำหนด (เช่น 30 นาที)
        """
        timeout_minutes = 30
        cutoff_time = fields.Datetime.now() - datetime.timedelta(minutes=timeout_minutes)

        expired_txs = self.search([
            ('provider_code', '=', 'scb'),
            ('state', '=', 'pending'),
            ('create_date', '<', cutoff_time)
        ])

        _logger.info(">>> SCB Cron Cleanup: Cancelling %s expired payment(s).", len(expired_txs))

        for tx in expired_txs:
            tx._add_scb_log("Transaction expired and cancelled by cleanup cron", 'info')
            tx._set_canceled(_("Payment expired (Timeout %s mins).") % timeout_minutes)
            tx.write({'scb_qr_image': False})

    def action_view_so(self):
        self.ensure_one()
        if self.sale_order_ids:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'sale.order',
                'res_id': self.sale_order_ids[0].id,
                'view_mode': 'form',
                'target': 'current',
            }

    def _create_scb_audit_log(self, payload):
        self.ensure_one()
        try:
            # ดึงยอดเงินแบบปลอดภัย
            amount_val = payload.get('amount')
            try:
                amount = float(amount_val) if amount_val else 0.0
            except (ValueError, TypeError):
                amount = 0.0

            # สร้าง Log
            log = self.env['scb.payment.log'].sudo().create({
                'name': payload.get('transRef') or payload.get('transactionId') or 'N/A',
                'order_ref': self.reference,
                'amount': amount,
                'scb_ref1': payload.get('ref1'),
                'scb_ref2': payload.get('ref2'),
                'raw_payload': str(payload),
                'transaction_id': self.id,
                'sale_order_id': self.sale_order_ids[0].id if self.sale_order_ids else False,
                'state': 'processed'
            })
            return log
        except Exception as e:
            _logger.error(">>> SCB Audit Log Error: %s", str(e))
            return False