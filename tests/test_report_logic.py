import unittest
from app import build_report

def pages(text):
    return [{"page": 1, "text": text}]

class ReportLogicTests(unittest.TestCase):
    def test_money_relationship(self):
        r = build_report(pages("Total project cost: ₹10,00,000. Equipment: ₹8,00,000. Installation: ₹2,00,000."))
        self.assertTrue(any("₹10,00,000 equals ₹8,00,000 plus ₹2,00,000" in x["text"] for x in r["amount_relationships"]))

    def test_warranty_keyword_alone_is_not_green(self):
        r = build_report(pages("Warranty: 5 years."))
        item = next(x for x in r["findings"] if x["label"] == "Warranty")
        self.assertEqual(item["status"], "Needs clarification")

    def test_warranty_with_specifics_is_found(self):
        r = build_report(pages("Warranty: 5 years covering parts and labour from commissioning."))
        item = next(x for x in r["findings"] if x["label"] == "Warranty")
        self.assertEqual(item["status"], "Found")

    def test_red_flag_wording(self):
        r = build_report(pages("Equivalent or similar make may be supplied. Freight will be extra and charged at actuals."))
        labels = {x["label"] for x in r["red_flags"]}
        self.assertIn("Substitution wording", labels)
        self.assertIn("Extra / actuals charges", labels)


    def test_before_you_pay_priorities_use_actual_findings(self):
        r = build_report(pages("Freight will be extra and charged at actuals. Warranty: 5 years."))
        self.assertEqual(r["before_you_pay"][0]["label"], "Extra / actuals charges")
        self.assertTrue(r["before_you_pay"][0]["action"])
        self.assertLessEqual(len(r["before_you_pay"]), 3)

    def test_payment_exposure_counts_only_early_stages(self):
        r = build_report(pages("Total project cost: ₹10,00,000. Payment: 40% advance, 30% on delivery, 20% on installation, 10% on commissioning."))
        self.assertEqual(r["payment_exposure"]["payment_percentage_before_delivery_or_work"], 40.0)
        self.assertEqual(r["payment_exposure"]["payment_amount_before_delivery_or_work"], 400000.0)
        self.assertEqual([x["stage"] for x in r["payment_percentages"]], ["Before delivery/work", "Delivery/work stage", "Delivery/work stage", "Delivery/work stage"])

    def test_payment_percentages_are_listed(self):
        r = build_report(pages("Payment: 40% advance, 30% on delivery, 20% on installation, 10% on commissioning."))
        self.assertEqual([x["percentage"] for x in r["payment_percentages"]], [40.0, 30.0, 20.0, 10.0])
        self.assertEqual(r["payment_exposure"]["payment_percentage_before_delivery_or_work"], 100.0)

if __name__ == "__main__":
    unittest.main()
