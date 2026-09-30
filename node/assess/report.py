"""PDF reports for a risk assessment (fpdf2, core fonts, MedChain colours)."""

from __future__ import annotations

import time
import unicodedata

from fpdf import FPDF

INDIGO = (0x23, 0x43, 0x86)
CREAM = (0xFB, 0xF9, 0xF6)
GOLD = (0xFF, 0xC4, 0x00)
ORANGE = (0xED, 0x73, 0x28)
SAGE = (0xA2, 0xD3, 0xA6)
SKY = (0x6A, 0xA8, 0xDC)
TRACK = (0xE6, 0xE2, 0xDA)
INK = (0, 0, 0)
WHITE = (255, 255, 255)

TIER_COLOURS = {"LOW": SAGE, "MODERATE": GOLD, "HIGH": ORANGE}
ADVICE = {
    "LOW": "Keep up a heart-healthy routine and have a routine check-up once a year.",
    "MODERATE": "Book a doctor's visit for a full cardiac work-up and start on the modifiable risk factors.",
    "HIGH": "Arrange a prompt cardiology consultation; do not wait for the next routine visit.",
}
RECOMMENDATIONS = {
    "LOW": [
        "Aim for at least 150 minutes of moderate exercise a week.",
        "Eat plenty of vegetables, fruit, pulses and whole grains; keep salt and fried food low.",
        "Check blood pressure, cholesterol and blood sugar at your yearly check-up.",
    ],
    "MODERATE": [
        "See a physician for a cardiovascular evaluation, including a lipid profile.",
        "Work towards a blood pressure below 130/80 mmHg, with medication if your doctor advises it.",
        "Cut saturated fat, salt and processed food; add 30 minutes of brisk activity on most days.",
        "If you smoke, get help to stop now.",
    ],
    "HIGH": [
        "Get a cardiologist's evaluation as soon as possible and bring this report with you.",
        "Avoid strenuous exertion until a doctor has cleared you.",
        "Take any prescribed medication as directed; do not stop it without advice.",
        "Chest pain, breathlessness, fainting or sweating at rest are an emergency: call 112 or go to the nearest emergency department.",
    ],
}
DISCLAIMER = ("This report is produced by a machine-learning model for information only. It is not a diagnosis "
              "and does not replace a qualified doctor's assessment; always consult a physician about medical decisions.")
LEVELS = {1: "Normal", 2: "Above normal", 3: "Well above normal"}


class _Report(FPDF):
    def __init__(self, title: str, subtitle: str):
        super().__init__(format="A4")
        self.set_margins(15, 15, 15)
        self.set_auto_page_break(auto=True, margin=18)
        self.add_page()
        self.set_fill_color(*INDIGO)
        self.rect(0, 0, 210, 34, "F")
        self.set_xy(15, 9)
        self.set_text_color(*WHITE)
        self.set_font("Helvetica", "B", 18)
        self.cell(0, 9, title.upper())
        self.set_xy(15, 20)
        self.set_font("Helvetica", "", 9)
        self.cell(0, 6, subtitle)
        self.set_text_color(*INK)
        self.set_y(42)

    def tag(self, text: str, colour: tuple, x: float, y: float) -> None:
        self.set_xy(x, y)
        self.set_fill_color(*colour)
        self.set_font("Helvetica", "B", 10)
        self.cell(44, 8, text, fill=True, align="C")

    def section(self, title: str) -> None:
        self.ln(3)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*INDIGO)
        self.cell(0, 7, title.upper(), new_x="LMARGIN", new_y="NEXT", align="L")
        self.set_draw_color(*INK)
        self.set_line_width(0.2)
        self.line(15, self.get_y(), 195, self.get_y())
        self.set_text_color(*INK)
        self.ln(2)

    def row(self, key: str, value: str, shade: bool) -> None:
        self.set_fill_color(*(CREAM if shade else WHITE))
        self.set_font("Helvetica", "", 10)
        self.cell(62, 7, f"  {key}", fill=True)
        self.set_font("Helvetica", "B", 10)
        self.multi_cell(0, 7, value, fill=True, new_x="LMARGIN", new_y="NEXT", align="L")

    def rows(self, pairs: list[tuple[str, str]]) -> None:
        for i, (k, v) in enumerate(pairs):
            self.row(k, v, i % 2 == 0)

    def bar(self, label: str, pct: float, colour: tuple, marker: float | None = None) -> None:
        y = self.get_y()
        self.set_font("Helvetica", "", 9)
        self.cell(55, 7, f"  {label}")
        x, w = self.get_x(), 105
        self.set_fill_color(*TRACK)
        self.rect(x, y + 2, w, 3.5, "F")
        self.set_fill_color(*colour)
        if pct > 0:
            self.rect(x, y + 2, w * min(pct, 100) / 100, 3.5, "F")
        if marker is not None:
            self.set_draw_color(*INK)
            self.set_line_width(0.4)
            self.line(x + w * marker / 100, y + 0.8, x + w * marker / 100, y + 6.7)
        self.set_xy(x + w + 3, y)
        self.set_font("Helvetica", "B", 9)
        self.cell(0, 7, f"{pct:.1f}%", new_x="LMARGIN", new_y="NEXT", align="L")

    def bullets(self, items: list[str], empty: str = "None identified") -> None:
        self.set_font("Helvetica", "", 10)
        for item in items or [empty]:
            self.set_x(18)
            self.multi_cell(0, 6, f"-  {item}", new_x="LMARGIN", new_y="NEXT", align="L")

    def footer_block(self, provenance: list[str]) -> None:
        self.section("About the model")
        self.set_font("Helvetica", "", 8.5)
        for line in provenance:
            self.multi_cell(0, 5, line, new_x="LMARGIN", new_y="NEXT", align="L")
        self.ln(3)
        self.set_fill_color(*CREAM)
        self.set_font("Helvetica", "I", 8.5)
        self.multi_cell(0, 5, f"Disclaimer: {DISCLAIMER}", fill=True, new_x="LMARGIN", new_y="NEXT", align="L")

    def bytes(self) -> bytes:
        return bytes(self.output())


def _latin1(text: str) -> str:
    return unicodedata.normalize("NFKC", text).encode("latin-1", "replace").decode("latin-1")


def _stamp(hospital: str) -> str:
    return f"{_latin1(hospital)}   |   Generated {time.strftime('%d %b %Y, %H:%M UTC', time.gmtime())}"


def tabular_report(hospital: str, patient: dict, result: dict, model: dict) -> bytes:
    tier = result["risk_tier"]
    pdf = _Report("Cardiovascular risk assessment", _stamp(hospital))
    pdf.tag(f"{tier} RISK", TIER_COLOURS[tier], 151, 11)

    pdf.set_y(42)
    pdf.section("1. Summary")
    pdf.rows([
        ("Result", result["label"]),
        ("Risk tier", f"{tier} RISK"),
        ("Blended probability", f"{result['confidence']:.1f}%  (positive at {result['threshold'] * 100:.0f}% or more)"),
        ("Advice", ADVICE[tier]),
    ])

    pdf.section("2. Patient inputs")
    pdf.rows([
        ("Age", f"{patient['age']:g} years"),
        ("Sex", "Male" if patient["gender"] == 2 else "Female"),
        ("Height / weight", f"{patient['height']:g} cm / {patient['weight']:g} kg  (BMI {result['bmi']:.1f})"),
        ("Blood pressure", f"{patient['ap_hi']:g}/{patient['ap_lo']:g} mmHg"),
        ("Cholesterol", LEVELS[int(patient["cholesterol"])]),
        ("Glucose", LEVELS[int(patient["gluc"])]),
        ("Smoking / alcohol", f"{'Yes' if patient['smoke'] else 'No'} / {'Yes' if patient['alco'] else 'No'}"),
        ("Physically active", "Yes" if patient["active"] else "No"),
    ])

    pdf.section("3. Model breakdown")
    marker = result["threshold"] * 100
    pdf.bar("Random forest", result["rf_confidence"], SKY)
    pdf.bar("XGBoost (calibrated)", result["xgb_confidence"], INDIGO)
    pdf.bar("Blended (0.4 RF + 0.6 XGB)", result["confidence"], TIER_COLOURS[tier], marker=marker)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, "  The black tick marks the decision threshold.", new_x="LMARGIN", new_y="NEXT", align="L")

    pdf.section("4. Risk factors")
    for name, key in (("Critical", "critical_factors"), ("Moderate", "moderate_factors"), ("Lifestyle", "lifestyle_factors")):
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, name, new_x="LMARGIN", new_y="NEXT", align="L")
        pdf.bullets(result[key])

    pdf.section("5. Recommendations")
    pdf.bullets(RECOMMENDATIONS[tier])

    m = model.get("metrics", {})
    pdf.footer_block([
        f"{model['architecture']}, trained on the {model['trained_on']}. This is a general, international "
        "dataset, not Indian-sourced data.",
        f"Held-out accuracy {m.get('accuracy', 0) * 100:.1f}% at the {result['threshold'] * 100:.0f}% threshold, "
        f"ROC-AUC {m.get('roc_auc', 0):.3f} (n = {m.get('n_test', 0):,}). Tiers: below threshold LOW; "
        "positive at 85% or more HIGH, otherwise MODERATE.",
    ])
    return pdf.bytes()


def ecg_report(hospital: str, filename: str, result: dict, model: dict) -> bytes:
    pdf = _Report("ECG image analysis", _stamp(hospital))
    pdf.set_y(42)
    pdf.section("1. Result")
    pdf.rows([
        ("Classification", result["label"]),
        ("Confidence", f"{result['confidence']:.1f}%"),
        ("ECG file", _latin1(filename) or "upload"),
    ])
    pdf.section("2. Class probabilities")
    p = result["probabilities"]
    pdf.bar("Normal ECG", p["normal"] * 100, SAGE)
    pdf.bar("Myocardial infarction", p["mi"] * 100, ORANGE)
    pdf.bar("Abnormal heartbeat", p["abnormal"] * 100, GOLD)
    m = model.get("metrics", {})
    pdf.footer_block([
        f"{model['architecture']}, trained on the {model['trained_on']}. Images come from one cardiology "
        "institute in Multan, Pakistan; they are 12-lead printouts, so photos or single-lead strips are out of range.",
        f"Held-out accuracy {m.get('accuracy', 0) * 100:.1f}% (n = {m.get('n_test', 0)}).",
    ])
    return pdf.bytes()
