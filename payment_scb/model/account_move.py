from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    def action_post(self):
        """
        เมื่อผู้ใช้กด Confirm (Post) Invoice แบบ Manual
        ให้ตรวจสอบว่ามี Transaction ที่จ่ายเงินไว้แล้วรอ Reconcile หรือไม่
        """
        res = super(AccountMove, self).action_post()

        for move in self:
            if move.move_type == 'out_invoice' and move.state == 'posted':
                # ค้นหา Transaction ของ SCB ที่สำเร็จแล้ว (done)
                # และเชื่อมโยงกับ Sale Order เดียวกับ Invoice ใบนี้
                sale_orders = move.line_ids.mapped('sale_line_ids.order_id')
                if sale_orders:
                    matching_txs = self.env['payment.transaction'].sudo().search([
                        ('sale_order_ids', 'in', sale_orders.ids),
                        ('provider_code', '=', 'scb'),
                        ('state', '=', 'done')
                    ])

                    for tx in matching_txs:
                        _logger.info(">>> SCB: Found matching TX %s for Manual Invoice %s", tx.reference, move.name)
                        # เรียกใช้ฟังก์ชัน Reconcile ที่คุณเขียนไว้
                        tx._reconcile_after_done()

        return res