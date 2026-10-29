"""Read-only analytical candidates and impact diagnostics for the main page."""

from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .analytical_model import CandidateResult


class ImpactPreview(QWidget):
    def __init__(self):
        super().__init__()
        self.candidate = None
        self.setMinimumHeight(140)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f1f5f9"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self.candidate
        if c is None or c.impact is None:
            painter.drawText(12, 25, "Kandidat mit berechneten Auftreffpunkten auswählen.")
            return
        impact = c.impact
        normal_index = "xyz".index(c.mount.normal_axis)
        transverse = 2 if normal_index == 1 else 1
        points = [(p[0], p[transverse]) for p in impact.points_m]
        all_points = [p for ring in impact.silhouette_rings for p in ring] + points
        radius = impact.jet_radius_m
        xmin, xmax = min(p[0] for p in all_points) - radius, max(p[0] for p in all_points) + radius
        ymin, ymax = min(p[1] for p in all_points) - radius, max(p[1] for p in all_points) + radius
        scale = min(
            (self.width() - 45) / max(xmax - xmin, 1e-9),
            (self.height() - 45) / max(ymax - ymin, 1e-9),
        )

        x_origin = (self.width() - (xmax - xmin) * scale) / 2

        def point(x, y):
            return QPointF(x_origin + (x - xmin) * scale, self.height() - 25 - (y - ymin) * scale)

        painter.setBrush(QColor("#cbd5e1"))
        painter.setPen(QPen(QColor("#334155"), 1))
        # Odd-even fill preserves holes in the projected silhouette.
        path = QPainterPath()
        for ring in impact.silhouette_rings:
            polygon = QPolygonF([point(x, y) for x, y in ring])
            path.addPolygon(polygon)
        path.setFillRule(Qt.FillRule.OddEvenFill)
        painter.drawPath(path)
        for i, ((x, y), weight) in enumerate(zip(points, impact.weights, strict=True), 1):
            color = QColor("#15803d" if weight == 1 else "#d97706" if weight == 0.5 else "#dc2626")
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(QColor(color.red(), color.green(), color.blue(), 55))
            painter.drawEllipse(point(x, y), radius * scale, radius * scale)
            painter.drawText(point(x, y), str(i))
        painter.setPen(QColor("#334155"))
        painter.drawText(
            12,
            16,
            f"Projektion X/{'XYZ'[transverse]} · grün: 1 · orange: 0,5 · rot: 0",
        )


class CalculationResultsWidget(QWidget):
    selection_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.results = ()
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            [
                "Array/Düse",
                "Achse/Winkel",
                "Puls [ms]",
                "Treffergewicht Σw",
                "Offset [mm]",
                "Überdruck [mbar]",
                "Absolut [bar]",
                "Status",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(105)
        self.table.setMaximumHeight(160)
        layout.addWidget(self.table)
        diagnostics = QSplitter()
        self.preview = ImpactPreview()
        diagnostics.addWidget(self.preview)
        self.details = QTextBrowser()
        diagnostics.addWidget(self.details)
        diagnostics.setStretchFactor(0, 1)
        diagnostics.setStretchFactor(1, 1)
        layout.addWidget(diagnostics, 2)
        self.table.itemSelectionChanged.connect(self.select_result)

    def set_results(self, candidates: tuple[CandidateResult, ...]):
        self.table.blockSignals(True)
        self.results = candidates
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(candidates))
        self.preview.candidate = None
        self.preview.update()
        self.details.clear()
        for row, c in enumerate(candidates):
            values = [
                f"{c.mount.array_index}/{c.mount.nozzle_index}",
                f"{c.turn.axis.upper()} {c.turn.signed_angle_deg:g}°",
                str(c.pulse_ms),
                f"{sum(c.impact.weights):g}" if c.impact else "—",
                f"{c.impact.offset_m * 1000:.4f}" if c.impact else "—",
                f"{c.pressure.gauge_pa / 100:.4g}" if c.pressure else "—",
                f"{c.pressure.absolute_pa / 100000:.5g}" if c.pressure else "—",
                c.message,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, col, item)
        if candidates:
            self.table.selectRow(0)
        self.table.blockSignals(False)
        self.select_result()

    def selected_candidate(self) -> CandidateResult | None:
        row = self.table.currentRow()
        return (
            self.results[row]
            if self.table.selectionModel().hasSelection() and 0 <= row < len(self.results)
            else None
        )

    def select_result(self):
        c = self.selected_candidate()
        if c is None:
            self.preview.candidate = None
            self.preview.update()
            self.details.clear()
            self.selection_changed.emit()
            return
        self.preview.candidate = c
        self.preview.update()

        def vector(values, factor=1000):
            return "(" + ", ".join(f"{v * factor:.6g}" for v in values) + ")"

        lines = [f"Array {c.mount.array_index}, Düse {c.mount.nozzle_index}: {c.message}"]
        if c.mechanics:
            m = c.mechanics
            lines += [
                f"Kippkante [mm]: {vector(m.edge_m[0])} → {vector(m.edge_m[1])}",
                f"J um Kippachse [kg·m²]: {m.inertia_axis_kg_m2:.8g}",
                f"Δh [m]: {m.delta_h_m:.8g}; Kipppunkt [°]: {m.tipping_angle_deg:.6g}",
                f"Schwerpunkt [mm]: {vector(c.center_m)}",
                "Schwerpunkttensor [kg·m²]:\n"
                + "\n".join(vector(row, 1) for row in c.inertia_cm_kg_m2),
            ]
        if c.impact:
            i = c.impact
            lines += [
                f"Treffervektor (Flächenüberdeckung, 0 / 0,5 / 1): {i.weights}",
                f"Summe der Treffergewichte: {sum(i.weights):g}; "
                f"Öffnungen mit positivem Gewicht: {sum(w > 0 for w in i.weights)}",
                f"Vollständig abgedeckt einschließlich Offset-Randabstand: {i.full_weights}",
                "Gewicht 0,5 trägt die halbe Strahlfläche bei. "
                "Die Treffergrenzen stehen unter Modellparameter.",
                f"Hebelarm zur Kippkante [mm]: {i.lever_arm_m * 1000:.6g}",
                f"A_eff [mm²]: {i.area_eff_m2 * 1e6:.6g}; "
                f"Strahlradius [mm]: {i.jet_radius_m * 1000:.6g}",
            ]
            lines.append(
                "Auftreffpunkte P und Abstandsvektoren ΔM zum geometrischen Mittelpunkt, "
                "ΔS zum Schwerpunkt; alle Koordinaten X/Y/Z in mm:"
            )
            for number, (point, weight, center, com) in enumerate(
                zip(i.points_m, i.weights, i.center_offsets_m, i.com_offsets_m, strict=True), 1
            ):
                lines.append(
                    f"Öffnung {number}: w={weight:g}; P={vector(point)}; "
                    f"ΔM={vector(center)}; ΔS={vector(com)}"
                )
                if i.coverage_fractions:
                    lines.append(f"  Flächenüberdeckung: {i.coverage_fractions[number - 1]:.2%}")
        if c.pressure:
            p = c.pressure
            lines += [
                f"Mittlere erforderliche Kraft [N]: {p.force_n:.8g}",
                f"p₀ absolut [Pa]: {p.absolute_pa:.8g}; p_g Überdruck [Pa]: {p.gauge_pa:.8g}",
                "Grenze nicht abgeschnürter Strömung [mbar Überdruck]: "
                f"{p.critical_gauge_pa / 100:.6g}",
            ]
        self.details.setPlainText("\n".join(lines))
        self.selection_changed.emit()
