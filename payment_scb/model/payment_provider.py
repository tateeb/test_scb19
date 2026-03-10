# -*- coding: utf-8 -*-
import uuid
import logging
import requests
from datetime import timedelta
from odoo import models, fields, _, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)



class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('scb', 'SCB Payment Gateway')],
        ondelete={'scb': 'set default'}
    )

    # === SCB Credentials ===
    scb_api_key = fields.Char(string='SCB API Key', required_if_provider='scb' , is_password='True')
    scb_api_secret = fields.Char(string='SCB API Secret', required_if_provider='scb' , is_password='True')
    scb_biller_id = fields.Char(string='SCB Biller ID', required_if_provider='scb' , is_password='True')
    scb_merchant_id = fields.Char(string='Merchant ID' ,required_if_provider='scb' , is_password='True')
    scb_terminal_id = fields.Char(string='Terminal ID' ,required_if_provider='scb' , is_password='True')
    scb_callback_url = fields.Char(
        string='Callback URL',
        compute='_compute_scb_callback_url',
        help="Endpoint URL designated for receiving asynchronous payment notifications (Webhooks) from SCB."
    )

    # === Token cache ===
    scb_access_token = fields.Char(string='SCB Access Token', readonly=True, copy=False)
    scb_token_expired_at = fields.Datetime(string='SCB Token Expired At', readonly=True, copy=False)

    scb_environment = fields.Selection([
        ('sandbox', 'Sandbox'),
        ('production', 'Production')
    ], string='Environment', default='Production', required=True)

    # === SCB Base URL Management (ปรับจากแบบ BBL ให้เป็น SCB) ===
    scb_base_url = fields.Char(
        string='SCB Base URL',
        compute='_compute_scb_base_url',
        readonly=False,
        store=True
    )
    scb_oauth_url = fields.Char(string='OAuth URL', compute='_compute_scb_urls', store=True)
    scb_api_url_qr = fields.Char(string='QR Create URL', compute='_compute_scb_urls', store=True)
    scb_api_url_qr_inquiry = fields.Char(string='QR Inquiry URL', compute='_compute_scb_urls', store=True)
    scb_ref3_prefix = fields.Char(string='Reference 3 Prefix', default='ODOO', help="ตัวย่อสำหรับระบุที่มาของรายการ")
    scb_account_name = fields.Char(string='Account Name')


    @api.depends('scb_environment')
    def _compute_scb_base_url(self):
        for rec in self:
            if rec.scb_environment == 'production':
                rec.scb_base_url = "https://api.partners.scb/partners"
            else:
                rec.scb_base_url = "https://api-sandbox.partners.scb/partners/sandbox"

    @api.depends('scb_base_url')
    def _compute_scb_urls(self):
        for rec in self:
            base = (rec.scb_base_url or "").rstrip('/')
            if base:
                rec.scb_oauth_url = f"{base}/v1/oauth/token"
                rec.scb_api_url_qr = f"{base}/v1/payment/qrcode/create"
                # แก้ไข: เติม / ระหว่าง base และ v1
                rec.scb_api_url_qr_inquiry = f"{base}/v1/payment/billpayment/inquiry"
            else:
                rec.scb_oauth_url = False
                rec.scb_api_url_qr = False
                rec.scb_api_url_qr_inquiry = False

    @api.depends('scb_environment')
    def _compute_scb_callback_url(self):
        """ คำนวณ URL โดยดึง Base URL ของ Odoo แล้วต่อท้ายด้วย Route ของเรา """
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for rec in self:
            # สมมติว่าคุณสร้าง Controller ไว้ที่ /payment/scb/webhook
            rec.scb_callback_url = f"{base_url.rstrip('/')}/payment/scb/webhook"

    # ==========================
    # OAuth: Get SCB Access Token
    # ==========================
    def _scb_get_access_token(self):
        self.ensure_one()
        _logger.info(">>> SCB [DEBUG 1]: Starting Token Request Process")

        # 1. ตรวจสอบ Cache (ถ้ายังไม่หมดอายุ ไม่ต้องขอใหม่)
        now = fields.Datetime.now()
        if self.scb_access_token and self.scb_token_expired_at:
            # เผื่อเวลาไว้ 2 นาที (120 วินาที) ป้องกัน Token ตายระหว่างใช้งาน
            if now < (self.scb_token_expired_at - timedelta(seconds=120)):
                _logger.info(">>> SCB: Using cached Access Token")
                return self.scb_access_token

        # 2. ตรวจสอบ Credentials
        if not self.scb_api_key or not self.scb_api_secret:
            _logger.error(">>> SCB: API Key or Secret is missing")
            return False

        endpoint = self.scb_oauth_url
        _logger.info(">>> SCB [DEBUG 2]: Target URL: %s", endpoint)
        _logger.info(">>> SCB [DEBUG 3]: Using Key: %s", self.scb_api_key)
        headers = {
            "Content-Type": "application/json",
            "resourceOwnerId": self.scb_api_key,
            "requestUId": str(uuid.uuid4()),
            "accept-language": "EN",
        }

        payload = {
            "applicationKey": self.scb_api_key,
            "applicationSecret": self.scb_api_secret
        }

        try:
            _logger.info(">>> SCB: Requesting New Access Token...")
            r = requests.post(endpoint, json=payload, headers=headers, timeout=20)
            _logger.info(">>> SCB [DEBUG 4]: HTTP Status: %s | Response: %s", r.status_code, r.text)
            res = r.json()

            if r.status_code == 200 and res.get('status', {}).get('code') == 1000:
                data = res.get('data', {})
                token = data.get('accessToken')
                expires_in = data.get('expiresIn', 3600)

                # บันทึกค่าลงฐานข้อมูล (ใช้ sudo เพื่อเลี่ยงปัญหา Permission ของลูกค้าหน้าเว็บ)
                self.sudo().write({
                    "scb_access_token": token,
                    "scb_token_expired_at": fields.Datetime.now() + timedelta(seconds=int(expires_in)),
                })
                _logger.info(">>> SCB: Token refreshed successfully")
                return token
            else:
                error_desc = res.get('status', {}).get('description', 'Unknown Error')
                _logger.error(">>> SCB OAuth Error: %s", error_desc)
                return False

        except Exception as e:
            _logger.error(">>> SCB Connection Error: %s", str(e))
            return False

    def action_scb_get_access_token(self):
        """ ปุ่มกดสำหรับ Admin ในหน้าตั้งค่า """
        self.ensure_one()
        token = self._scb_get_access_token()
        if token:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('SCB Online'),
                    'message': _('เชื่อมต่อสำเร็จ! ได้รับ Access Token เรียบร้อยแล้ว'),
                    'type': 'success',
                    'sticky': False,
                }
            }
        raise UserError(_("ไม่สามารถขอ Token ได้ กรุณาตรวจสอบ API Key/Secret หรือ Log ในระบบ"))

    def _get_payment_flow(self):
        """ กำหนด Flow เป็น Redirect เพื่อไปหน้าแสดง QR Code """
        if self.code == 'scb':
            return 'redirect'
        return super()._get_payment_flow()