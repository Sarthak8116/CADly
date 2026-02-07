"""Per-process cost estimation for FDM, SLA, CNC, and Injection Molding."""

import math
import logging

from src.models.costs import CostEstimate

logger = logging.getLogger(__name__)

# ---- Cost constants ----

# FDM (desktop/pro-sumer printers)
FDM_MATERIAL_PER_CM3 = 0.04  # $/cm3 (PLA/ABS avg)
FDM_MACHINE_PER_HR = 6.0  # $/hr incl. electricity + depreciation + overhead
FDM_PRINT_RATE_CM3_HR = 18.0  # cm3/hr at ~20% infill
FDM_HANDLING_HRS_PER_PART = 0.03  # post-processing per part (supports/cleanup)

# SLA (desktop/resin)
SLA_MATERIAL_PER_CM3 = 0.18  # $/cm3 (standard resin)
SLA_MACHINE_PER_HR = 8.0  # $/hr
SLA_PRINT_RATE_CM3_HR = 25.0  # cm3/hr (layer-based)
SLA_HANDLING_HRS_PER_PART = 0.04  # wash + cure per part

# CNC (3-axis job shop)
CNC_MACHINE_PER_HR = 90.0  # $/hr (machine + operator)
CNC_SETUP_TIME_HRS = 1.5  # programming + fixturing per batch
CNC_BATCH_SIZE = 50  # parts per setup batch
CNC_MATERIAL_PER_CM3 = 0.02  # $/cm3 (aluminum 6061 stock + waste)
CNC_REMOVAL_RATE_CM3_HR = 40.0  # cm3/hr material removal
CNC_LOAD_UNLOAD_HRS = 0.05  # load/unload per part

# Injection Molding (single-cavity to multi-cavity)
IM_BASE_TOOLING = 8000.0  # $ base mold cost
IM_COMPLEXITY_FACTOR = 1200.0  # $ per face (complexity proxy)
IM_MAX_TOOLING = 150000.0  # $ cap
IM_MATERIAL_PER_CM3 = 0.015  # $/cm3 (ABS/PP pellets)
IM_CYCLE_TIME_BASE_S = 20.0  # seconds per shot (base)
IM_MACHINE_PER_HR = 70.0  # $/hr machine time
IM_RUNNER_WASTE_FACTOR = 1.05  # extra material for runners/sprue


class CostEstimator:
    """Estimate manufacturing costs per process based on part geometry."""

    def estimate_all(
        self,
        volume_cm3: float,
        area_cm2: float,
        bounding_box: dict | None = None,
        face_count: int = 0,
        quantity: int = 1,
    ) -> list[CostEstimate]:
        """Calculate cost estimates for all manufacturing processes."""
        bb = bounding_box or {}
        return [
            self.estimate_fdm(volume_cm3, quantity=quantity),
            self.estimate_sla(volume_cm3, quantity=quantity),
            self.estimate_cnc(volume_cm3, bb, quantity=quantity),
            self.estimate_im(volume_cm3, face_count, quantity=quantity),
        ]

    # ---- FDM ----

    def estimate_fdm(self, volume_cm3: float, quantity: int = 1) -> CostEstimate:
        """FDM: material cost + machine time. Scales linearly with quantity."""
        material_rate = FDM_MATERIAL_PER_CM3 * self._material_discount(quantity)
        material_cost = volume_cm3 * material_rate * quantity
        print_time = (volume_cm3 / FDM_PRINT_RATE_CM3_HR) * quantity
        print_time *= self._batch_time_multiplier(quantity, floor=0.85)
        time_hrs = print_time + (FDM_HANDLING_HRS_PER_PART * quantity)
        time_cost = time_hrs * FDM_MACHINE_PER_HR
        return CostEstimate(
            process="FDM",
            material_cost=material_cost,
            machine_time_hrs=time_hrs,
            time_cost=time_cost,
            setup_cost=0.0,
            total_cost=material_cost + time_cost,
            quantity=quantity,
        )

    # ---- SLA ----

    def estimate_sla(self, volume_cm3: float, quantity: int = 1) -> CostEstimate:
        """SLA: resin cost + machine time. Scales linearly with quantity."""
        material_rate = SLA_MATERIAL_PER_CM3 * self._material_discount(quantity)
        material_cost = volume_cm3 * material_rate * quantity
        print_time = (volume_cm3 / SLA_PRINT_RATE_CM3_HR) * quantity
        print_time *= self._batch_time_multiplier(quantity, floor=0.8)
        time_hrs = print_time + (SLA_HANDLING_HRS_PER_PART * quantity)
        time_cost = time_hrs * SLA_MACHINE_PER_HR
        return CostEstimate(
            process="SLA",
            material_cost=material_cost,
            machine_time_hrs=time_hrs,
            time_cost=time_cost,
            setup_cost=0.0,
            total_cost=material_cost + time_cost,
            quantity=quantity,
        )

    # ---- CNC ----

    def estimate_cnc(
        self,
        volume_cm3: float,
        bounding_box: dict,
        quantity: int = 1,
    ) -> CostEstimate:
        """CNC: machine time + material + one-time setup cost."""
        stock_vol = self._bounding_box_volume(bounding_box)
        material_rate = CNC_MATERIAL_PER_CM3 * self._material_discount(quantity)
        material_cost = stock_vol * material_rate * quantity
        removal_vol = max(0.0, stock_vol - volume_cm3)
        time_per_part = max(removal_vol / CNC_REMOVAL_RATE_CM3_HR, 0.2)
        time_per_part += CNC_LOAD_UNLOAD_HRS
        time_hrs = time_per_part * quantity
        time_cost = time_hrs * CNC_MACHINE_PER_HR
        # Setup per batch
        batches = max(1, math.ceil(quantity / CNC_BATCH_SIZE))
        setup_cost = CNC_SETUP_TIME_HRS * CNC_MACHINE_PER_HR * batches
        return CostEstimate(
            process="CNC",
            material_cost=material_cost,
            machine_time_hrs=time_hrs,
            time_cost=time_cost,
            setup_cost=setup_cost,
            total_cost=material_cost + time_cost + setup_cost,
            quantity=quantity,
        )

    # ---- Injection Molding ----

    def estimate_im(
        self,
        volume_cm3: float,
        face_count: int = 0,
        quantity: int = 1,
    ) -> CostEstimate:
        """Injection molding: high tooling + low per-unit cost."""
        tooling_cost = min(
            IM_BASE_TOOLING + face_count * IM_COMPLEXITY_FACTOR,
            IM_MAX_TOOLING,
        )
        material_rate = IM_MATERIAL_PER_CM3 * self._material_discount(quantity)
        material_cost = volume_cm3 * material_rate * IM_RUNNER_WASTE_FACTOR * quantity
        cavities = self._im_cavities(quantity)
        cycle_s = IM_CYCLE_TIME_BASE_S + volume_cm3 * 0.25
        cycle_s = max(12.0, cycle_s) / cavities
        time_hrs = (cycle_s / 3600.0) * quantity
        time_cost = time_hrs * IM_MACHINE_PER_HR
        # Tooling is one-time, amortized over quantity in unit_cost
        setup_cost = tooling_cost
        return CostEstimate(
            process="Injection Molding",
            material_cost=material_cost,
            machine_time_hrs=time_hrs,
            time_cost=time_cost,
            setup_cost=setup_cost,
            total_cost=material_cost + time_cost + setup_cost,
            quantity=quantity,
        )

    # ---- Helpers ----

    @staticmethod
    def _bounding_box_volume(bb: dict) -> float:
        """Calculate bounding box volume in cm3."""
        bb_min = bb.get("min", [0, 0, 0])
        bb_max = bb.get("max", [1, 1, 1])
        return abs(
            (bb_max[0] - bb_min[0])
            * (bb_max[1] - bb_min[1])
            * (bb_max[2] - bb_min[2])
        )

    @staticmethod
    def _material_discount(quantity: int) -> float:
        """Simple volume pricing discount for bulk material."""
        if quantity >= 50000:
            return 0.85
        if quantity >= 10000:
            return 0.9
        if quantity >= 1000:
            return 0.95
        return 1.0

    @staticmethod
    def _batch_time_multiplier(quantity: int, floor: float = 0.85) -> float:
        """Small efficiency gain for batching on additive machines."""
        if quantity <= 5:
            return 1.0
        if quantity <= 20:
            return 0.95
        if quantity <= 100:
            return 0.9
        return floor

    @staticmethod
    def _im_cavities(quantity: int) -> int:
        """Approximate cavity count scaling for high-volume injection molding."""
        if quantity >= 50000:
            return 8
        if quantity >= 20000:
            return 4
        if quantity >= 5000:
            return 2
        return 1

    @staticmethod
    def get_recommendation(estimates: list[CostEstimate]) -> str:
        """Return the cheapest process name."""
        if not estimates:
            return "FDM"
        cheapest = min(estimates, key=lambda e: e.total_cost)
        return cheapest.process
