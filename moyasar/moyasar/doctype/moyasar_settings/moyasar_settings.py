# Copyright (c) 2026, QualityPoint and contributors
# For license information, please see license.txt

"""Moyasar (mada / Apple Pay / STC Pay) as a frappe/payments gateway.

Flow: Payment Request -> get_payment_url() creates a Moyasar hosted invoice and
an Integration Request that remembers it -> the customer pays on Moyasar ->
Moyasar redirects back to return_from_moyasar() and, separately, posts to
webhook(). Either one reaching us is enough; whichever arrives first settles it.

Neither the redirect nor the webhook body is trusted. Both only name an
invoice; _settle() fetches that invoice from Moyasar with the secret key and
acts on what Moyasar itself reports, checking amount and currency against what
we asked for.

Settling a Payment Request calls set_as_paid() directly instead of
on_payment_authorized(): webshop overrides the latter to do nothing for a Guest
session (every webhook) or while the cart is disabled (every subscription
payment), which would silently leave paid invoices unpaid.
"""

import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import frappe
import requests
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, flt, get_url

from moyasar.utils import as_administrator

API_BASE = "https://api.moyasar.com/v1/"
SERVICE = "Moyasar"
SUPPORTED_CURRENCIES = ("SAR",)
# Moyasar rejects anything below 1 SAR.
MINIMUM_AMOUNT = 1
METHOD_PATH = "moyasar.moyasar.doctype.moyasar_settings.moyasar_settings"
# A Moyasar invoice can't be paid after this. Apps that cancel unpaid orders
# can wait this long after the last payment attempt without racing a customer
# who is still on the payment page.
PAYMENT_LINK_MINUTES = 60


class MoyasarSettings(Document):
	@property
	def webhook_url(self):
		return get_url(f"/api/method/{METHOD_PATH}.webhook")

	def on_update(self):
		if not self.enabled:
			return
		from payments.utils import create_payment_gateway

		create_payment_gateway(SERVICE, settings="Moyasar Settings")
		# erpnext creates the Payment Gateway Account for the default company.
		call_hook_method("payment_gateway_enabled", gateway=SERVICE)

	def validate_transaction_currency(self, currency):
		if currency not in SUPPORTED_CURRENCIES:
			frappe.throw(
				_("Moyasar does not support transactions in {0}. Supported: {1}").format(
					currency, ", ".join(SUPPORTED_CURRENCIES)
				)
			)

	def validate_minimum_transaction_amount(self, currency, amount):
		if flt(amount) < MINIMUM_AMOUNT:
			frappe.throw(
				_("The minimum amount payable through Moyasar is {0} {1}").format(MINIMUM_AMOUNT, currency)
			)

	def get_payment_url(self, **kwargs):
		"""Create a Moyasar hosted invoice and return its payment page URL."""
		if not self.enabled:
			frappe.throw(_("Moyasar payments are disabled"))

		amount = _to_halalas(kwargs["amount"])
		integration_request = create_request_log({**kwargs, "amount_halalas": amount}, service_name=SERVICE)
		invoice = self._api(
			"POST",
			"invoices",
			json={
				"amount": amount,
				"currency": kwargs.get("currency") or "SAR",
				"description": (
					kwargs.get("description") or kwargs.get("title") or kwargs["reference_docname"]
				)[:255],
				"success_url": get_url(
					f"/api/method/{METHOD_PATH}.return_from_moyasar?ir={integration_request.name}"
				),
				"back_url": get_url(
					_order_page(kwargs.get("reference_doctype"), kwargs.get("reference_docname")) or "/"
				),
				"callback_url": self.webhook_url,
				"expired_at": (datetime.now(UTC) + timedelta(minutes=PAYMENT_LINK_MINUTES)).isoformat(
					timespec="seconds"
				),
				"metadata": {
					"integration_request": integration_request.name,
					"reference_doctype": kwargs.get("reference_doctype"),
					"reference_docname": kwargs.get("reference_docname"),
				},
			},
		)
		integration_request.db_set("request_id", invoice["id"])
		return invoice["url"]

	def _api(self, method, path, **kwargs):
		response = requests.request(
			method,
			API_BASE + path,
			auth=(self.get_password("secret_key"), ""),
			timeout=20,
			**kwargs,
		)
		if not response.ok:
			frappe.log_error(
				title="Moyasar API error",
				message=f"{method} {path} -> {response.status_code}\n{response.text}",
			)
			frappe.throw(_("Could not reach the payment provider. Please try again."))
		return response.json()


def _order_page(reference_doctype, reference_docname):
	"""The portal page of the Sales Order a Payment Request is for, if any."""
	if reference_doctype != "Payment Request" or not reference_docname:
		return None
	ref = frappe.db.get_value(
		"Payment Request", reference_docname, ["reference_doctype", "reference_name"], as_dict=True
	)
	if ref and ref.reference_doctype == "Sales Order":
		return f"/orders/{quote(ref.reference_name)}"
	return None


def _to_halalas(amount):
	return round(flt(amount) * 100)


def _settle(integration_request_name):
	"""Settle one Integration Request against Moyasar's own record. Idempotent."""
	ir = frappe.get_doc("Integration Request", integration_request_name, for_update=True)
	# Only Completed is final: a declined card leaves the invoice open for a retry.
	if ir.integration_request_service != SERVICE or ir.status == "Completed":
		return ir
	if not ir.request_id:
		return ir

	invoice = frappe.get_single("Moyasar Settings")._api("GET", f"invoices/{ir.request_id}")
	status = invoice.get("status")
	if status != "paid":
		if status in ("canceled", "expired", "voided"):
			ir.db_set({"status": "Failed", "error": json.dumps(invoice)})
		return ir

	expected = json.loads(ir.data)
	if invoice.get("amount") != expected.get("amount_halalas") or invoice.get("currency") != (
		expected.get("currency") or "SAR"
	):
		ir.db_set({"status": "Failed", "error": json.dumps(invoice)})
		frappe.log_error(
			title="Moyasar amount mismatch",
			message=f"{ir.name}: expected {expected.get('amount_halalas')} {expected.get('currency')}, "
			f"got {invoice.get('amount')} {invoice.get('currency')}",
		)
		return ir

	ir.db_set({"status": "Completed", "output": json.dumps(invoice)})

	# Reviewed: settling runs as Guest (webhook) or as the paying portal user,
	# neither of whom may submit the Payment Entry that set_as_paid() makes.
	# Reached only after Moyasar itself confirmed the amount was paid.
	with as_administrator():
		ref = frappe.get_doc(ir.reference_doctype, ir.reference_docname)
		if ref.doctype == "Payment Request" and ref.docstatus == 2:
			# The order was cancelled while the link was still open: the money
			# arrived for nothing and must go back.
			frappe.log_error(
				title="Moyasar payment on a cancelled order - refund needed",
				message=f"{ir.name} paid Moyasar invoice {ir.request_id} for cancelled {ref.name}.",
			)
		elif ref.doctype == "Payment Request":
			if ref.status != "Paid":
				ref.set_as_paid()
			else:
				# A retried checkout opens a fresh Moyasar invoice; paying two of them
				# for one request is a real double charge that someone must refund.
				frappe.log_error(
					title="Moyasar duplicate payment - refund needed",
					message=f"{ir.name} paid Moyasar invoice {ir.request_id} for {ref.name}, which was already paid.",
				)
		else:
			ref.run_method("on_payment_authorized", "Completed")
	return ir


@frappe.whitelist(allow_guest=True, methods=["GET"])  # nosemgrep: guest-whitelisted-method
def return_from_moyasar(ir: str, **kwargs):
	"""Moyasar's success_url. Settles the payment, then shows the outcome."""
	if not frappe.db.exists("Integration Request", ir):
		frappe.throw(_("Unknown payment"), frappe.DoesNotExistError)

	doc = _settle(ir)
	# Required, not defensive: this is a GET because Moyasar redirects the
	# browser here, and frappe rolls back every GET. Without the commit a paid
	# invoice reverts to unpaid. Not a CSRF risk: _settle() only acts on what
	# Moyasar reports for the invoice, and is idempotent.
	frappe.db.commit()  # nosemgrep: frappe-manual-commit, whitelisted-side-effect-on-get

	order_page = _order_page(doc.reference_doctype, doc.reference_docname)
	if order_page:
		# A webshop order: back to its own page in the customer portal, which
		# shows the outcome, rather than the payments app's unbranded pages.
		outcome = "failed" if doc.status == "Failed" else "success"
		location = f"{order_page}?payment={outcome}"
	elif doc.status == "Completed":
		location = f"/payment-success?doctype={doc.reference_doctype}&docname={doc.reference_docname}"
	elif doc.status == "Failed":
		location = "/payment-failed"
	else:
		# Paid on Moyasar's side but not yet reported as such: the webhook will settle it.
		location = f"/payment-success?doctype={doc.reference_doctype}&docname={doc.reference_docname}"
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = get_url(location)


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep: guest-whitelisted-method
def webhook(**kwargs):
	"""Moyasar webhook (payment_paid). Authenticated by the shared secret token."""
	payload = frappe.request.get_json(silent=True) or {}
	secret = frappe.get_single("Moyasar Settings").get_password("webhook_secret", raise_exception=False)
	if not secret or not hmac.compare_digest(str(payload.get("secret_token") or ""), secret):
		frappe.throw(_("Invalid webhook token"), frappe.AuthenticationError)

	data = payload.get("data") or {}
	invoice_id = data.get("invoice_id") or (
		data.get("id") if payload.get("type", "").startswith("invoice") else None
	)
	ir_name = invoice_id and frappe.db.get_value(
		"Integration Request", {"integration_request_service": SERVICE, "request_id": invoice_id}
	)
	if ir_name:
		_settle(ir_name)
	# Acknowledge unknown invoices too, so Moyasar does not retry them forever.
	return {"ok": True}
