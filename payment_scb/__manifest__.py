{
    'name': 'SCB Promptpay Payment Gateway ',
    'version': '1.0',
    'category': 'Accounting/Payment',
    'summary': 'SCB Promptpay Payment Gateway Plugin allows merchants to accept Promptpay Payment for SCB(Siam Commercial Bank)',
    'depends': ['base', 'payment', 'website_sale','account', 'sale'],
    'data': [
        'security/ir.model.access.csv',
        'data/payment_provider_data.xml',
        'views/payment_provider_views.xml',
        'views/payment_templates.xml',
        'views/payment_history_views.xml',

        'views/payment_transaction_views.xml',
        'data/cron_data.xml',


    ],

    'installable': True,
    'application': True,
}