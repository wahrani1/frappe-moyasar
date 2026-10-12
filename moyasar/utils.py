# Copyright (c) 2026, QualityPoint and contributors
# For license information, please see license.txt

from contextlib import contextmanager

import frappe


@contextmanager
def as_administrator():
	"""Run a block as Administrator without touching the visitor's session.

	frappe.set_user() rewrites the session in place (its sid becomes the user
	name and its data, csrf token included, is dropped), and frappe writes
	session.sid back into the browser's cookie at the end of the request -- so
	switching back with set_user() logs the visitor out. Elevate on a copy of
	the session and put the original object back afterwards.
	"""
	local = frappe.local
	session, form_dict = local.session, local.form_dict
	local.session = frappe._dict(session)
	frappe.set_user("Administrator")  # nosemgrep: frappe-setuser
	try:
		yield
	finally:
		local.session, local.form_dict = session, form_dict
		local.user_perms = None
		local.role_permissions = {}
		local.cache = {}
