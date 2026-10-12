### Moyasar

[Moyasar](https://moyasar.com) payment gateway for Frappe / ERPNext: customers pay
Payment Requests (web shop orders, subscription invoices, anything with a Payment
Request) by mada, Apple Pay, STC Pay or card, in SAR.

It plugs into the standard [payments](https://github.com/frappe/payments) framework as
the Payment Gateway **Moyasar**, so ERPNext's Payment Request flow, webshop checkout
and subscriptions use it like any built-in gateway.

#### How a payment is confirmed

1. A Payment Request asks for a payment URL: a Moyasar hosted **invoice** is created
   (valid for 60 minutes) and an Integration Request remembers it.
2. The customer pays on Moyasar's page.
3. Moyasar redirects the customer back (`return_from_moyasar`) and, separately, calls
   the webhook. Either is enough.
4. Neither is trusted: the app fetches the invoice from Moyasar with the secret key and
   only if Moyasar reports it **paid, for the amount and currency asked**, marks the
   Payment Request paid (Payment Entry, and Sales Invoice when the request asks for
   one). Settling is idempotent; a second payment for an already-paid or cancelled
   request is logged in Error Log as needing a refund.

#### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/wahrani1/frappe-moyasar --branch develop
bench --site <site> install-app moyasar
```

Requires `payments` and `erpnext`.

#### Setup

1. **Moyasar Settings**: enter the Publishable Key and Secret Key (`pk_test_…` /
   `sk_test_…` in sandbox), tick **Enabled**, save. This creates the Payment Gateway
   "Moyasar" and, for the default company, a Payment Gateway Account.
2. **Payment Gateway Account** "Moyasar - SAR - …": set **Payment Account** to a
   non-group bank/clearing ledger (e.g. "Moyasar"). A group account makes every
   settlement fail.
3. **Webhook** (production): in the Moyasar dashboard add the *Webhook URL* shown in
   Moyasar Settings for the `payment_paid` event, and copy its Secret Token into
   **Webhook Secret**. Without it a payment is still confirmed when the customer
   returns to the site; the webhook covers customers who close the tab. It needs a
   publicly reachable site, so it never fires on a local bench.

Only SAR is supported; Moyasar's minimum is 1.00 SAR.

#### License

MIT
