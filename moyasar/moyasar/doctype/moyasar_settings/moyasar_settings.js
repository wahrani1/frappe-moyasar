// Copyright (c) 2026, QualityPoint and contributors
// For license information, please see license.txt

frappe.ui.form.on("Moyasar Settings", {
	refresh(frm) {
		frm.set_intro(
			__(
				"Payments are confirmed by fetching the invoice back from Moyasar, so a missing webhook only delays confirmation until the customer returns to the site."
			)
		);
	},
});
