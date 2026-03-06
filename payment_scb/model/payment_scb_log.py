from odoo import models, fields, api
from odoo.exceptions import UserError


class SCBPaymentLog(models.Model):
    _name = 'scb.payment.log'
    _description = 'SCB QR Raw Payment Logs'
    _order = 'create_date desc'

    name = fields.Char(
        string='Bank Reference',
        readonly=True,
        required=True,
        index=True
    )

    order_ref = fields.Char(string='Order Reference', readonly=True, index=True)
    amount = fields.Float(string='Amount', readonly=True)

    scb_ref1 = fields.Char(string='Reference 1 (Ref1)')
    scb_ref2 = fields.Char(string='Reference 2 (Ref2)')

    raw_payload = fields.Text(string='Raw Payload (JSON)', readonly=True)

    transaction_id = fields.Many2one(
        'payment.transaction',
        string='Related Transaction',
        index=True
    )

    sale_order_id = fields.Many2one(
        'sale.order',
        string='Related Sale Order',
        index=True
    )

    state = fields.Selection([
        ('received', 'Received'),
        ('processed', 'Processed'),
        ('error', 'Error')
    ], default='received', index=True)

    error_message = fields.Text(string='Error Message')

    _sql_constraints = [
        ('scb_name_unique', 'unique(name)', 'Bank Reference must be unique.')
    ]

    def action_view_raw_data(self):
        self.ensure_one()
        preview = (self.raw_payload or '')[:3000]
        raise UserError(f"ข้อมูลดิบจากธนาคาร:\n\n{preview}")