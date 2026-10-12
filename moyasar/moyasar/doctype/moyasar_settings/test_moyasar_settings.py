# Copyright (c) 2026, QualityPoint and contributors
# See license.txt

"""The money rule: nothing is marked paid on our word or the caller's --
only on Moyasar's own record of the invoice, for the amount we asked for."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from moyasar.moyasar.doctype.moyasar_settings import moyasar_settings as ms

API = "moyasar.moyasar.doctype.moyasar_settings.moyasar_settings.MoyasarSettings._api"


def _invoice(**kwargs):
	return {"id": "inv_test", "status": "paid", "amount": 15050, "currency": "SAR", **kwargs}


class TestMoyasarSettings(FrappeTestCase):
	def setUp(self):
		# Registered first so it also runs when setUp itself fails.
		self.addCleanup(frappe.db.rollback)
		# create_request_log() commits; without this every test leaks rows past the rollback.
		self.enterContext(patch.object(frappe.db, "commit"))
		settings = frappe.get_single("Moyasar Settings")
		settings.update(
			{"publishable_key": "pk_test_x", "secret_key": "sk_test_x", "webhook_secret": "whsec"}
		)
		settings.flags.ignore_mandatory = True
		# enabled stays off here so on_update does not create gateway records;
		# get_payment_url's enabled guard is flipped per-test.
		settings.save()
		self.ref = frappe.get_doc({"doctype": "ToDo", "description": "moyasar test"}).insert()

	def _start(self, amount=150.5):
		settings = frappe.get_single("Moyasar Settings")
		settings.enabled = 1
		with patch(API, return_value={"id": "inv_test", "url": "https://pay.example/inv_test"}) as api:
			url = settings.get_payment_url(
				amount=amount,
				currency="SAR",
				title="Skystar",
				reference_doctype="ToDo",
				reference_docname=self.ref.name,
			)
		return url, api

	def _ir(self):
		return frappe.get_last_doc("Integration Request", filters={"request_id": "inv_test"})

	def test_payment_url_creates_invoice_in_halalas(self):
		url, api = self._start()
		self.assertEqual(url, "https://pay.example/inv_test")
		payload = api.call_args.kwargs["json"]
		self.assertEqual(payload["amount"], 15050)
		self.assertIn(self._ir().name, payload["success_url"])

	def test_paid_invoice_settles_once(self):
		self._start()
		ir = self._ir()
		with patch(API, return_value=_invoice()) as api:
			self.assertEqual(ms._settle(ir.name).status, "Completed")
			ms._settle(ir.name)
		self.assertEqual(api.call_count, 1, "a settled payment must not be re-processed")

	def test_amount_mismatch_is_not_settled(self):
		self._start()
		with patch(API, return_value=_invoice(amount=100)):
			self.assertEqual(ms._settle(self._ir().name).status, "Failed")

	def test_unpaid_invoice_stays_open(self):
		self._start()
		for status in ("initiated", "failed"):
			with patch(API, return_value=_invoice(status=status)):
				self.assertEqual(ms._settle(self._ir().name).status, "Queued")

	def test_payment_request_is_marked_paid_directly(self):
		self._start()
		ir = self._ir()
		ir.db_set({"reference_doctype": "Payment Request", "reference_docname": "PR-TEST"})
		fake = frappe._dict(doctype="Payment Request", status="Initiated")
		calls = []
		fake.set_as_paid = lambda: calls.append(frappe.session.user)
		real_get_doc = frappe.get_doc

		def get_doc(*args, **kwargs):
			return fake if args[:2] == ("Payment Request", "PR-TEST") else real_get_doc(*args, **kwargs)

		with patch(API, return_value=_invoice()), patch.object(frappe, "get_doc", get_doc):
			ms._settle(ir.name)
		self.assertEqual(calls, ["Administrator"])

	def test_webhook_rejects_bad_token(self):
		frappe.local.request = frappe._dict(get_json=lambda silent=True: {"secret_token": "nope", "data": {}})
		self.assertRaises(frappe.AuthenticationError, ms.webhook)
