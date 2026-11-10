"""Read-only analytical candidates and impact diagnostics for the main page."""

import math

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF, QTransform
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

    def visible_opening_indices(self) -> tuple[int, ...]:
        """Only powered nozzle rows contribute to the drawing and its bounds."""
        if self.candidate is None or self.candidate.impact is None:
            return ()
        impact = self.candidate.impact
        if impact.opening_ids:
            return tuple(
                index
                for index, (nozzle, _) in enumerate(impact.opening_ids)
                if nozzle in impact.active_nozzles
            )
        if self.candidate.mount.nozzle_index in impact.active_nozzles:
            return tuple(range(len(impact.points_m)))
        return ()

    def _projection_transform(self, points, radius):
        """Display +X to the left, matching the machine's observed belt travel."""
        xmin, xmax = min(p[0] for p in points) - radius, max(p[0] for p in points) + radius
        ymin, ymax = min(p[1] for p in points) - radius, max(p[1] for p in points) + radius
        scale = min(
            (self.width() - 45) / max(xmax - xmin, 1e-9),
            (self.height() - 95) / max(ymax - ymin, 1e-9),
        )
        x_origin = (self.width() - (xmax - xmin) * scale) / 2
        return QTransform(
            -scale, 0, 0, -scale, x_origin + xmax * scale, self.height() - 60 + ymin * scale
        )

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
        indices = self.visible_opening_indices()
        if not indices:
            painter.drawText(12, 25, "Keine eingeschalteten Düsen vorhanden.")
            return
        points = [(impact.points_m[i][0], impact.points_m[i][transverse]) for i in indices]
        all_points = [p for ring in impact.silhouette_rings for p in ring] + points
        radius = impact.jet_radius_m
        transform = self._projection_transform(all_points, radius)
        scale = abs(transform.m11())

        def point(x, y):
            return transform.map(QPointF(x, y))

        painter.setBrush(QColor("#cbd5e1"))
        painter.setPen(QPen(QColor("#334155"), 1))
        # Odd-even fill preserves holes in the projected silhouette.
        path = QPainterPath()
        for ring in impact.silhouette_rings:
            polygon = QPolygonF([point(x, y) for x, y in ring])
            path.addPolygon(polygon)
        path.setFillRule(Qt.FillRule.OddEvenFill)
        painter.drawPath(path)
        for i, (x, y) in zip(indices, points, strict=True):
            weight = impact.weights[i]
            color = QColor("#15803d" if weight == 1 else "#d97706" if weight == 0.5 else "#dc2626")
            opening = impact.opening_ids[i] if impact.opening_ids else (c.mount.nozzle_index, i + 1)
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(QColor(color.red(), color.green(), color.blue(), 55))
            painter.drawEllipse(point(x, y), radius * scale, radius * scale)
            painter.drawText(point(x, y), f"{opening[0]}.{opening[1]}")
        painter.setPen(QColor("#334155"))
        painter.drawText(
            12,
            16,
            f"Projektion X/{'XYZ'[transverse]} · grün: 1 · orange: 0,5 · rot: 0 · nur aktive Düsen",
        )
        # Screen-space annotation: keep arrow and label clear of the scaled geometry.
        left, right = self.width() * 0.3, self.width() * 0.7
        y = self.height() - 18
        painter.setPen(QPen(QColor("#334155"), 2))
        painter.drawLine(QPointF(right, y), QPointF(left, y))
        painter.drawLine(QPointF(left, y), QPointF(left + 9, y - 5))
        painter.drawLine(QPointF(left, y), QPointF(left + 9, y + 5))
        painter.drawText(
            QRectF(0, y - 30, self.width(), 24),
            Qt.AlignmentFlag.AlignCenter,
            "Förderbandrichtung (+X)",
        )


class CalculationResultsWidget(QWidget):
    selection_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.results = ()
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            [
                "Array / aktive Düsen",
                "Achse/Winkel",
                "Puls [ms]",
                "Treffergewicht Σw",
                "Offset [mm]",
                "Analytischer Überdruck [mbar]",
                "SPS-Sollwert [mbar]",
                "Analytisch absolut [bar]",
                "Kraft [N]",
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
                f"Array {c.mount.array_index} · {c.nozzle_label}",
                f"{c.turn.axis.upper()} {c.turn.signed_angle_deg:g}°",
                str(c.pulse_ms),
                f"{sum(c.impact.weights):g}" if c.impact else "—",
                f"{c.impact.offset_m * 1000:.4f}" if c.impact else "—",
                f"{c.pressure.gauge_pa / 100:.4g}" if c.pressure else "—",
                f"{c.sps_pressure.gauge_pa / 100:.6g}" if c.sps_pressure else "—",
                f"{c.pressure.absolute_pa / 100000:.5g}" if c.pressure else "—",
                f"{c.pressure.force_n:.6g}" if c.pressure else "—",
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

        lines = [f"Array {c.mount.array_index}, {c.nozzle_label}: {c.message}"]
        lines.append("Förderbandrichtung: +X (im Trefferbild von rechts nach links)")
        lines.append(f"Fester Offsetbezug: Referenzdüse {c.mount.nozzle_index}")
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
            if i.opening_ids:
                for nozzle in sorted({n for n, _ in i.opening_ids}):
                    indices = [j for j, (n, _) in enumerate(i.opening_ids) if n == nozzle]
                    full_hits = sum(i.full_weights[j] for j in indices)
                    area = sum(i.weights[j] for j in indices) * math.pi * i.jet_radius_m**2
                    reason = (
                        "EIN: mindestens ein Volltreffer einschließlich Randabstand"
                        if nozzle in i.active_nozzles
                        else "AUS: kein Volltreffer einschließlich Randabstand"
                    )
                    lines.append(
                        f"Düse {nozzle}: {reason}; Volltreffer: {full_hits:g}; "
                        f"wirksame Fläche [mm²]: {area * 1e6:.6g}"
                    )
            lines += [
                f"Zusätzliche Offsetreserve entlang X [mm]: {i.offset_safety_m * 1000:g}",
                f"Wirksamer Treffervektor (0 / 0,5 / 1): {i.weights}",
                f"Geometrische Treffergewichte vor Düsenwahl: {i.geometric_weights}",
                f"Summe der Treffergewichte: {sum(i.weights):g}; "
                f"Öffnungen mit positivem Gewicht: {sum(w > 0 for w in i.weights)}",
                f"Vollständig abgedeckt einschließlich Offset-Randabstand: {i.full_weights}",
                "Gewicht 0,5 trägt die halbe Strahlfläche bei. "
                "Die Treffergrenzen stehen unter Modellparameter.",
                f"Wirksamer Hebelarm zur Kippkante [mm]: {i.lever_arm_m * 1000:.6g}",
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
                nozzle, opening = (
                    i.opening_ids[number - 1] if i.opening_ids else (c.mount.nozzle_index, number)
                )
                lines.append(
                    f"Düse {nozzle}, Öffnung {opening}: w={weight:g}; P={vector(point)}; "
                    f"ΔM={vector(center)}; ΔS={vector(com)}"
                )
                if i.coverage_fractions:
                    lines.append(f"  Flächenüberdeckung: {i.coverage_fractions[number - 1]:.2%}")
                if i.lever_arms_m:
                    lines.append(f"  Einzelhebelarm [mm]: {i.lever_arms_m[number - 1] * 1000:.6g}")
        if c.pressure:
            p = c.pressure
            lines += [
                f"Mittlere erforderliche Kraft [N]: {p.force_n:.8g}",
                f"p₀ absolut [Pa]: {p.absolute_pa:.8g}; p_g Überdruck [Pa]: {p.gauge_pa:.8g}",
                "Grenze nicht abgeschnürter Strömung [mbar Überdruck]: "
                f"{p.critical_gauge_pa / 100:.6g}",
            ]
        if c.pressure and c.sps_pressure:
            sps = c.sps_pressure
            lines += [
                f"Analytischer Überdruck [mbar]: {c.pressure.gauge_pa / 100:.10g}",
                f"SPS-Sollwert [mbar]: {sps.gauge_pa / 100:.10g}",
                "SPS-Druckskalierung (Drücke in mbar): "
                f"({c.pressure.gauge_pa / 100:.10g} − ({sps.scale_b_pa / 100:.10g})) "
                f"/ {sps.scale_a:.12g} = {sps.gauge_pa / 100:.10g}",
                sps.source,
            ]
        self.details.setPlainText("\n".join(lines))
        self.selection_changed.emit()
