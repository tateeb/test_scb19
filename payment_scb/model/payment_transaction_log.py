from odoo import models, fields


class PaymentTransactionLog(models.Model):
    _name = 'payment.transaction.log'
    _description = 'Payment Transaction Log'
    _order = 'create_date desc'

    # ฟิลด์ที่เป็นตัวเชื่อม (นี่คือตัวที่ทำให้เกิด KeyError ถ้าไม่มี)
    transaction_id = fields.Many2one('payment.transaction', string='Transaction', ondelete='cascade', index=True)

    # ฟิลด์เก็บข้อมูล Log
    log_type = fields.Selection([
        ('info', 'Info'),
        ('error', 'Error'),
        ('request', 'Request'),
        ('response', 'Response')
    ], string='Type', default='info')

    message = fields.Char(string='Message')
    payload = fields.Text(string='Payload/Data')
    create_date = fields.Datetime(string='Created On', default=fields.Datetime.now)