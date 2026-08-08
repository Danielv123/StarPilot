from __future__ import annotations

import bz2
import base64
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Sequence
from collections import Counter, deque
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import struct
import subprocess
from typing import Any

try:
  import capnp
  import zstandard as zstd
  from cereal import log as capnp_log
except ModuleNotFoundError as exc:
  raise RuntimeError(
    "The rlog adapter requires pycapnp and zstandard. "
    + "From the StarPilot checkout, run it with: "
    + "uv run --no-project --with pycapnp==2.1.0 --with zstandard "
    + "python -m services.comma_companion.adapters.rlog ..."
  ) from exc


CONTRACT_NAME = "comma-companion.telemetry"
CONTRACT_VERSION = 1
EXTRACTOR_VERSION = "1.1.0"
DYNAMICS_SCHEMA = "comma-companion.dynamics-row"
DYNAMICS_SCHEMA_VERSION = 1
DYNAMICS_SAMPLE_PERIOD_US = 10_000
DYNAMICS_REQUIRED_SOURCE_MAX_AGE_US = 35_000
MAX_ROUTE_SEGMENTS = 512
MAX_SOURCE_BYTES_PER_SEGMENT = 64 * 1024 * 1024
MAX_SOURCE_BYTES_PER_ROUTE = 4 * 1024 * 1024 * 1024
MAX_DECOMPRESSED_BYTES_PER_SEGMENT = 128 * 1024 * 1024
MAX_DECOMPRESSED_BYTES_PER_ROUTE = 16 * 1024 * 1024 * 1024
MAX_MESSAGES_PER_SEGMENT = 500_000
MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_CAPNP_SEGMENTS_PER_MESSAGE = 128
READ_CHUNK_BYTES = 1024 * 1024
LIVE_PARAMETERS_MAX_AGE_US = 250_000
LIVE_TORQUE_MAX_AGE_US = 1_000_000
LIVE_TORQUE_ALIVE_TIMEOUT_US = 2_500_000
LIVE_TORQUE_MIN_FREQUENCY_HZ = 3.2
LIVE_TORQUE_MAX_FREQUENCY_HZ = 4.8
EVENT_MERGE_MAX_OVERLAP_NS = 5_000_000_000
MAX_RETAINED_SEGMENT_PAYLOADS = 2
MAX_RETAINED_DECOMPRESSED_BYTES = MAX_RETAINED_SEGMENT_PAYLOADS * MAX_DECOMPRESSED_BYTES_PER_SEGMENT
MIN_CHUNK_SIZE = 16
MAX_CHUNK_SIZE = 16_384
MAX_RETAINED_UTC_ANCHORS = 100_000
MAX_OUTPUT_RECORDS = 10_000_000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024 * 1024
ACCELERATION_DUE_TO_GRAVITY = 9.80665
FF_ROLL_OFFSET_FADE_LOW_MPS = 0.5
FF_ROLL_OFFSET_FADE_HIGH_MPS = 2.5
POUND_TO_KILOGRAM = 0.453592
CONTROLLER_PARAM_ALLOWLIST = (
  "AdvancedLongitudinalTune",
  "AdvancedLateralTune",
  "FLMActiveOverrides",
  "FLMActiveProfileId",
  "FLMTrialApplied",
  "ForceAutoTune",
  "ForceAutoTuneOff",
  "ForceTorqueController",
  "LateralTune",
  "LiveTorqueParameters",
  "NNFF",
  "NNFFLite",
  "NNFFModelName",
  "SteerDelay",
  "SteerFriction",
  "SteerFrictionJerkGain",
  "SteerFrictionStock",
  "SteerKP",
  "SteerKPStock",
  "SteerLatAccel",
  "SteerLatAccelStock",
  "SteerRatio",
  "SteerRatioStock",
  "TrailerLoad",
  "TuningLevel",
  "TuningLevelConfirmed",
  "UseAutoSteerDelay",
)
RESOLVED_TOGGLE_ALLOWLIST = (
  "force_auto_tune",
  "force_auto_tune_off",
  "friction",
  "friction_jerk_gain",
  "latAccelFactor",
  "lateral_tune",
  "nnff",
  "nnff_lite",
  "nnff_model_name",
  "tuning_level",
  "use_custom_friction",
  "use_custom_latAccelFactor",
)
REQUIRED_RESOLVED_TORQUE_TOGGLE_KEYS = {
  "force_auto_tune": bool,
  "force_auto_tune_off": bool,
  "friction": (int, float),
  "latAccelFactor": (int, float),
  "nnff": bool,
  "nnff_lite": bool,
  "use_custom_friction": bool,
  "use_custom_latAccelFactor": bool,
}
CONTROLLER_SELECTION_EVALUATOR_NAME = "starpilot-controlsd-lateral-selection"
CONTROLLER_SELECTION_EVALUATOR_VERSION = 1
CONTROLLER_SELECTION_EVALUATOR_SOURCE_SHA256 = hashlib.sha256(
  b"starpilot-controlsd-lateral-selection:v1:" + b"select-once-at-controlsd-initialization:" + b"nnff-precedes-nnff_lite:" + b"conventional-torque-fallback",
).hexdigest()
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
BZ2_MAGIC = b"BZh"
SEGMENT_RE = re.compile(r"^(?P<route>.+)--(?P<segment>\d+)$")
TIER_WIDTHS_US = (100_000, 500_000, 2_000_000, 10_000_000)
CAMERA_SERVICES = {
  "roadEncodeIdx": ("road", "fcamera.hevc"),
  "wideRoadEncodeIdx": ("wide", "ecamera.hevc"),
  "driverEncodeIdx": ("driver", "dcamera.hevc"),
  "qRoadEncodeIdx": ("qcamera", "qcamera.ts"),
}


class InputError(ValueError):
  pass


class ExtractionError(RuntimeError):
  pass


class ResourceLimitError(ExtractionError):
  def __init__(self, code: str, limit: int, observed: int):
    self.code = code
    self.limit = limit
    self.observed = observed
    super().__init__(
      f"{code} resource limit exceeded: observed {observed}, limit {limit}",
    )


@dataclass(frozen=True)
class SegmentInput:
  number: int
  path: Path
  directory_name: str


@dataclass(frozen=True)
class RouteInput:
  route_id: str
  log_type: str
  segments: tuple[SegmentInput, ...]


@dataclass(frozen=True)
class SignalSpec:
  signal_id: str
  value_type: str
  unit: str | None
  source: str
  interpolation: str = "linear"
  downsample: str = "numeric_envelope"

  def as_dict(self) -> dict[str, Any]:
    return {
      "id": self.signal_id,
      "value_type": self.value_type,
      "unit": self.unit,
      "source": self.source,
      "interpolation": self.interpolation,
      "downsample": self.downsample,
    }


SIGNAL_SPECS = (
  SignalSpec("vehicle.speed", "float", "m/s", "carState.vEgo"),
  SignalSpec("vehicle.acceleration", "float", "m/s^2", "carState.aEgo"),
  SignalSpec("vehicle.speed_raw", "float", "m/s", "carState.vEgoRaw"),
  SignalSpec("vehicle.yaw_rate", "float", "rad/s", "carState.yawRate"),
  SignalSpec("vehicle.steering_angle", "float", "rad", "carState.steeringAngleDeg converted to radians"),
  SignalSpec("vehicle.steering_rate_abs", "float", "rad/s", "abs(carState.steeringRateDeg) converted to radians/s"),
  SignalSpec("vehicle.steering_rate_signed", "float", "rad/s", "causal derivative of carState.steeringAngleDeg"),
  SignalSpec("vehicle.steering_torque_driver", "float", "native", "carState.steeringTorque"),
  SignalSpec("vehicle.steering_torque_eps", "float", "native", "carState.steeringTorqueEps"),
  SignalSpec("vehicle.steering_pressed", "bool", None, "carState.steeringPressed", "step", "transitions"),
  SignalSpec("vehicle.steering_disengage", "bool", None, "carState.steeringDisengage", "step", "transitions"),
  SignalSpec("vehicle.steer_fault_temporary", "bool", None, "carState.steerFaultTemporary", "step", "transitions"),
  SignalSpec("vehicle.steer_fault_permanent", "bool", None, "carState.steerFaultPermanent", "step", "transitions"),
  SignalSpec("vehicle.gas_pressed", "bool", None, "carState.gasPressed", "step", "transitions"),
  SignalSpec("vehicle.brake_pressed", "bool", None, "carState.brakePressed", "step", "transitions"),
  SignalSpec("vehicle.standstill", "bool", None, "carState.standstill", "step", "transitions"),
  SignalSpec("vehicle.cruise_enabled", "bool", None, "carState.cruiseState.enabled", "step", "transitions"),
  SignalSpec("vehicle.cruise_set_speed", "float", "m/s", "carState.cruiseState.speed"),
  SignalSpec("vehicle.gear", "enum", None, "carState.gearShifter", "step", "transitions"),
  SignalSpec("vehicle.fuel_or_charge_fraction", "float", "1", "carState.fuelGauge"),
  SignalSpec("control.enabled", "bool", None, "carControl.enabled", "step", "transitions"),
  SignalSpec("control.lateral_active", "bool", None, "carControl.latActive", "step", "transitions"),
  SignalSpec("control.longitudinal_active", "bool", None, "carControl.longActive", "step", "transitions"),
  SignalSpec("control.torque_command", "float", "1", "carControl.actuators.torque"),
  SignalSpec("control.steering_angle_command", "float", "rad", "carControl.actuators.steeringAngleDeg converted to radians"),
  SignalSpec("control.curvature_command", "float", "1/m", "carControl.actuators.curvature"),
  SignalSpec("control.acceleration_command", "float", "m/s^2", "carControl.actuators.accel"),
  SignalSpec("control.torque_output_can", "float", "native", "carControl.actuators.torqueOutputCan"),
  SignalSpec("control.current_curvature", "float", "1/m", "carControl.currentCurvature"),
  SignalSpec("control.applied_torque", "float", "1", "carOutput.actuatorsOutput.torque"),
  SignalSpec("control.applied_steering_angle", "float", "rad", "carOutput.actuatorsOutput.steeringAngleDeg converted to radians"),
  SignalSpec("control.applied_curvature", "float", "1/m", "carOutput.actuatorsOutput.curvature"),
  SignalSpec("control.applied_acceleration", "float", "m/s^2", "carOutput.actuatorsOutput.accel"),
  SignalSpec("lateral.active", "bool", None, "controlsState.lateralControlState.torqueState.active", "step", "transitions"),
  SignalSpec("lateral.actual_acceleration", "float", "m/s^2", "controlsState.lateralControlState.torqueState.actualLateralAccel"),
  SignalSpec("lateral.desired_acceleration", "float", "m/s^2", "controlsState.lateralControlState.torqueState.desiredLateralAccel"),
  SignalSpec("lateral.desired_jerk", "float", "m/s^3", "controlsState.lateralControlState.torqueState.desiredLateralJerk"),
  SignalSpec("lateral.error", "float", "m/s^2", "controlsState.lateralControlState.torqueState.error"),
  SignalSpec("lateral.error_rate", "float", "m/s^3", "controlsState.lateralControlState.torqueState.errorRate"),
  SignalSpec("lateral.p", "float", "1", "controlsState.lateralControlState.torqueState.p"),
  SignalSpec("lateral.i", "float", "1", "controlsState.lateralControlState.torqueState.i"),
  SignalSpec("lateral.d", "float", "1", "controlsState.lateralControlState.torqueState.d"),
  SignalSpec("lateral.f", "float", "1", "controlsState.lateralControlState.torqueState.f"),
  SignalSpec("lateral.output", "float", "1", "controlsState.lateralControlState.torqueState.output"),
  SignalSpec("lateral.saturated", "bool", None, "controlsState.lateralControlState.torqueState.saturated", "step", "transitions"),
  SignalSpec("lateral.desired_curvature", "float", "1/m", "controlsState.desiredCurvature"),
  SignalSpec("lateral.measured_curvature", "float", "1/m", "controlsState.curvature"),
  SignalSpec("live_tune.valid", "bool", None, "liveTorqueParameters.liveValid", "step", "transitions"),
  SignalSpec("live_tune.in_use", "bool", None, "liveTorqueParameters.useParams", "step", "transitions"),
  SignalSpec("live_tune.lateral_accel_factor", "float", "m/s^2", "liveTorqueParameters.latAccelFactorFiltered"),
  SignalSpec("live_tune.lateral_accel_offset", "float", "m/s^2", "liveTorqueParameters.latAccelOffsetFiltered"),
  SignalSpec("live_tune.friction", "float", "1", "liveTorqueParameters.frictionCoefficientFiltered"),
  SignalSpec("live_tune.steer_ratio", "float", "1", "liveParameters.steerRatio"),
  SignalSpec("live_tune.stiffness_factor", "float", "1", "liveParameters.stiffnessFactor"),
  SignalSpec("live_tune.roll", "float", "rad", "liveParameters.roll"),
  SignalSpec("model.desired_curvature", "float", "1/m", "modelV2.action.desiredCurvature"),
  SignalSpec("model.desired_acceleration", "float", "m/s^2", "modelV2.action.desiredAcceleration"),
  SignalSpec("selfdrive.enabled", "bool", None, "selfdriveState.enabled", "step", "transitions"),
  SignalSpec("selfdrive.active", "bool", None, "selfdriveState.active", "step", "transitions"),
  SignalSpec("selfdrive.engageable", "bool", None, "selfdriveState.engageable", "step", "transitions"),
  SignalSpec("selfdrive.state", "enum", None, "selfdriveState.state", "step", "transitions"),
  SignalSpec("device.onroad", "bool", None, "deviceState.started", "step", "transitions"),
  SignalSpec("device.storage_free", "float", "1", "deviceState.freeSpacePercent divided by 100"),
  SignalSpec("device.memory_used", "float", "1", "deviceState.memoryUsagePercent divided by 100"),
  SignalSpec("device.cpu_used_mean", "float", "1", "mean(deviceState.cpuUsagePercent) divided by 100"),
  SignalSpec("device.cpu_temperature_max", "float", "degC", "max(deviceState.cpuTempC)"),
  SignalSpec("device.gpu_temperature_max", "float", "degC", "max(deviceState.gpuTempC)"),
  SignalSpec("device.power_draw", "float", "W", "deviceState.powerDrawW"),
  SignalSpec("device.network_type", "enum", None, "deviceState.networkType", "step", "transitions"),
  SignalSpec("device.network_strength", "enum", None, "deviceState.networkStrength", "step", "transitions"),
  SignalSpec("device.network_metered", "bool", None, "deviceState.networkMetered", "step", "transitions"),
  SignalSpec("gps.latitude", "float", "deg", "gpsLocation*.latitude"),
  SignalSpec("gps.longitude", "float", "deg", "gpsLocation*.longitude"),
  SignalSpec("gps.altitude", "float", "m", "gpsLocation*.altitude"),
  SignalSpec("gps.speed", "float", "m/s", "gpsLocation*.speed"),
  SignalSpec("gps.bearing", "float", "deg", "gpsLocation*.bearingDeg"),
  SignalSpec("gps.horizontal_accuracy", "float", "m", "gpsLocation*.horizontalAccuracy"),
  SignalSpec("gps.has_fix", "bool", None, "gpsLocation*.hasFix", "step", "transitions"),
  SignalSpec("gps_external.latitude", "float", "deg", "gpsLocationExternal.latitude"),
  SignalSpec("gps_external.longitude", "float", "deg", "gpsLocationExternal.longitude"),
  SignalSpec("gps_external.altitude", "float", "m", "gpsLocationExternal.altitude"),
  SignalSpec("gps_external.speed", "float", "m/s", "gpsLocationExternal.speed"),
  SignalSpec("gps_external.bearing", "float", "deg", "gpsLocationExternal.bearingDeg"),
  SignalSpec("gps_external.horizontal_accuracy", "float", "m", "gpsLocationExternal.horizontalAccuracy"),
  SignalSpec("gps_external.has_fix", "bool", None, "gpsLocationExternal.hasFix", "step", "transitions"),
)
SIGNAL_BY_ID = {spec.signal_id: spec for spec in SIGNAL_SPECS}
SCALAR_SERVICES = {
  "carState",
  "carControl",
  "carOutput",
  "controlsState",
  "liveTorqueParameters",
  "liveParameters",
  "modelV2",
  "selfdriveState",
  "deviceState",
  "gpsLocation",
  "gpsLocationExternal",
}

DYNAMICS_COLUMNS = (
  {"id": "t_us", "value_type": "int", "unit": "us", "source": "nominal 100 Hz clock"},
  {"id": "nominal_t_us", "value_type": "int", "unit": "us", "source": "nominal 100 Hz clock"},
  {"id": "nominal_log_mono_time_ns", "value_type": "decimal_string", "unit": "ns", "source": "absolute 100 Hz grid"},
  {"id": "source_t_us", "value_type": "int", "unit": "us", "source": "carState.logMonoTime"},
  {"id": "source_time_error_us", "value_type": "int", "unit": "us", "source": "-car_state_age_us"},
  {"id": "log_mono_time_ns", "value_type": "decimal_string", "unit": "ns", "source": "carState.logMonoTime"},
  {"id": "segment_num", "value_type": "int", "unit": None, "source": "containing segment"},
  {"id": "source_ordinal", "value_type": "int", "unit": None, "source": "record order within segment"},
  {"id": "continuous", "value_type": "bool", "unit": None, "source": "contiguous eligible 100 Hz grid row"},
  {"id": "quality_flags", "value_type": "list_text", "unit": None, "source": "row-level causal alignment limitations"},
  {"id": "car_state_age_us", "value_type": "int", "unit": "us", "source": "grid tick - latest causal carState"},
  {"id": "car_control_age_us", "value_type": "int", "unit": "us", "source": "grid tick - latest causal carControl"},
  {"id": "car_output_age_us", "value_type": "int", "unit": "us", "source": "grid tick - latest causal carOutput"},
  {"id": "controls_state_age_us", "value_type": "int", "unit": "us", "source": "grid tick - latest causal controlsState"},
  {"id": "live_torque_age_us", "value_type": "int_or_null", "unit": "us", "source": "grid tick - latest causal liveTorqueParameters"},
  {"id": "live_parameters_age_us", "value_type": "int_or_null", "unit": "us", "source": "grid tick - latest causal liveParameters"},
  {"id": "applied_torque", "value_type": "float", "unit": "1", "source": "carOutput.actuatorsOutput.torque"},
  {"id": "applied_torque_source", "value_type": "enum", "unit": None, "source": "carOutput.actuatorsOutput.torque"},
  {"id": "requested_torque", "value_type": "float_or_null", "unit": "1", "source": "carControl.actuators.torque"},
  {"id": "actual_lateral_accel", "value_type": "float", "unit": "m/s^2", "source": "torqueState.actualLateralAccel"},
  {"id": "steering_angle_deg", "value_type": "float", "unit": "deg", "source": "carState.steeringAngleDeg"},
  {"id": "steering_rate_deg", "value_type": "float", "unit": "deg/s", "source": "carState.steeringRateDeg"},
  {
    "id": "signed_steering_rate_deg_s",
    "value_type": "float",
    "unit": "deg/s",
    "source": "causal grid difference of zero-order-held steering angle",
  },
  {"id": "steering_torque_eps", "value_type": "float", "unit": "native", "source": "carState.steeringTorqueEps"},
  {"id": "v_ego", "value_type": "float", "unit": "m/s", "source": "carState.vEgo"},
  {"id": "a_ego", "value_type": "float", "unit": "m/s^2", "source": "carState.aEgo"},
  {"id": "desired_curvature", "value_type": "float", "unit": "1/m", "source": "carControl.actuators.curvature"},
  {"id": "controls_desired_curvature", "value_type": "float_or_null", "unit": "1/m", "source": "controlsState.desiredCurvature"},
  {"id": "desired_lateral_accel", "value_type": "float", "unit": "m/s^2", "source": "torqueState.desiredLateralAccel"},
  {"id": "desired_lateral_jerk", "value_type": "float", "unit": "m/s^3", "source": "torqueState.desiredLateralJerk"},
  {"id": "controller_output", "value_type": "float", "unit": "1", "source": "torqueState.output"},
  {"id": "controller_i", "value_type": "float", "unit": "1", "source": "selected causal controlsState.torqueState.i"},
  {"id": "controller_i_timing", "value_type": "enum", "unit": None, "source": "post-update controlsState snapshot selected as of grid tick"},
  {"id": "lat_active", "value_type": "bool", "unit": None, "source": "carControl.latActive"},
  {"id": "driver_overlay", "value_type": "bool", "unit": None, "source": "latActive and carState.steeringPressed"},
  {"id": "saturated", "value_type": "bool", "unit": None, "source": "torqueState.saturated"},
  {"id": "steer_limited_by_safety", "value_type": "bool", "unit": None, "source": "difference between requested and applied torque"},
  {"id": "integrator_frozen", "value_type": "bool", "unit": None, "source": "logged reconstructable freeze conditions"},
  {"id": "integrator_freeze_exact", "value_type": "bool", "unit": None, "source": "false when controller-internal unwind state is unavailable"},
  {"id": "live_torque_valid", "value_type": "bool", "unit": None, "source": "liveTorqueParameters.liveValid"},
  {"id": "live_torque_in_use", "value_type": "bool", "unit": None, "source": "liveTorqueParameters.useParams"},
  {"id": "live_torque_event_valid", "value_type": "bool_or_null", "unit": None, "source": "Event.valid"},
  {"id": "live_torque_alive", "value_type": "bool_or_null", "unit": None, "source": "causal timestamp cadence"},
  {"id": "live_torque_frequency_ok", "value_type": "bool_or_null", "unit": None, "source": "causal timestamp cadence"},
  {"id": "live_parameters_event_valid", "value_type": "bool_or_null", "unit": None, "source": "Event.valid"},
  {
    "id": "live_lat_accel_factor",
    "value_type": "float_or_null",
    "unit": "m/s^2 per normalized_torque",
    "source": "controlsState-bound liveTorqueParameters.latAccelFactorFiltered",
  },
  {"id": "live_lat_accel_offset", "value_type": "float_or_null", "unit": "m/s^2", "source": "controlsState-bound liveTorqueParameters.latAccelOffsetFiltered"},
  {"id": "live_friction", "value_type": "float_or_null", "unit": "1", "source": "controlsState-bound liveTorqueParameters.frictionCoefficientFiltered"},
  {"id": "base_lat_accel_factor", "value_type": "float", "unit": "m/s^2 per normalized_torque", "source": "carParams lateral torque tune"},
  {"id": "base_lat_accel_offset", "value_type": "float", "unit": "m/s^2", "source": "carParams lateral torque tune"},
  {"id": "base_friction", "value_type": "float", "unit": "1", "source": "carParams lateral torque tune"},
  {
    "id": "effective_lat_accel_factor",
    "value_type": "float_or_null",
    "unit": "m/s^2 per normalized_torque",
    "source": "versioned runtime torque-context evaluator",
  },
  {"id": "effective_lat_accel_offset", "value_type": "float_or_null", "unit": "m/s^2", "source": "versioned runtime torque-context evaluator"},
  {"id": "effective_friction", "value_type": "float_or_null", "unit": "1", "source": "versioned runtime torque-context evaluator"},
  {"id": "effective_torque_params_exact", "value_type": "bool", "unit": None, "source": "runtime context and cadence proof"},
  {"id": "controller_params_sha256", "value_type": "text_or_null", "unit": None, "source": "selected initData params snapshot"},
  {"id": "car_params_wire_sha256", "value_type": "text_or_null", "unit": None, "source": "selected full carParams snapshot"},
  {"id": "resolved_toggles_sha256", "value_type": "text_or_null", "unit": None, "source": "selected starpilotPlan.starpilotToggles JSON"},
  {"id": "steering_angle_deadzone_deg", "value_type": "float_or_null", "unit": "deg", "source": "carParams lateral torque tune"},
  {"id": "roll", "value_type": "float_or_null", "unit": "rad", "source": "controlsState-bound liveParameters.roll"},
  {
    "id": "gravity_adjusted_future_lateral_accel",
    "value_type": "float_or_null",
    "unit": "m/s^2",
    "source": "controls desired curvature times speed squared, minus faded roll compensation",
  },
  {"id": "future_feedforward_lateral_accel", "value_type": "float_or_null", "unit": "m/s^2", "source": "causal reconstruction before vehicle scaling"},
  {"id": "future_feedforward_params_source", "value_type": "enum_or_null", "unit": None, "source": "live_filtered or car_params"},
  {"id": "future_feedforward_eligible", "value_type": "bool", "unit": None, "source": "all direct reconstruction inputs were logged"},
  {"id": "future_feedforward_missing_fields", "value_type": "list_text", "unit": None, "source": "missing reconstruction inputs"},
  {"id": "future_feedforward_exact", "value_type": "bool", "unit": None, "source": "false when unlogged overrides may apply"},
  {"id": "car_state_log_mono_time_ns", "value_type": "decimal_string", "unit": "ns", "source": "selected carState event identity"},
  {"id": "car_state_segment_num", "value_type": "int", "unit": None, "source": "selected carState event identity"},
  {"id": "car_state_source_ordinal", "value_type": "int", "unit": None, "source": "selected carState event identity"},
  {"id": "car_control_log_mono_time_ns", "value_type": "decimal_string", "unit": "ns", "source": "selected carControl event identity"},
  {"id": "car_control_segment_num", "value_type": "int", "unit": None, "source": "selected carControl event identity"},
  {"id": "car_control_source_ordinal", "value_type": "int", "unit": None, "source": "selected carControl event identity"},
  {"id": "car_output_log_mono_time_ns", "value_type": "decimal_string", "unit": "ns", "source": "selected carOutput event identity"},
  {"id": "car_output_segment_num", "value_type": "int", "unit": None, "source": "selected carOutput event identity"},
  {"id": "car_output_source_ordinal", "value_type": "int", "unit": None, "source": "selected carOutput event identity"},
  {"id": "controls_state_log_mono_time_ns", "value_type": "decimal_string", "unit": "ns", "source": "selected controlsState event identity"},
  {"id": "controls_state_segment_num", "value_type": "int", "unit": None, "source": "selected controlsState event identity"},
  {"id": "controls_state_source_ordinal", "value_type": "int", "unit": None, "source": "selected controlsState event identity"},
  {"id": "car_params_scope", "value_type": "enum", "unit": None, "source": "carParams snapshot selection scope"},
  {"id": "car_params_log_mono_time_ns", "value_type": "decimal_string", "unit": "ns", "source": "selected carParams event identity"},
  {"id": "car_params_segment_num", "value_type": "int", "unit": None, "source": "selected carParams event identity"},
  {"id": "car_params_source_ordinal", "value_type": "int", "unit": None, "source": "selected carParams event identity"},
  {"id": "controller_params_log_mono_time_ns", "value_type": "decimal_string_or_null", "unit": "ns", "source": "selected initData event identity"},
  {"id": "controller_params_segment_num", "value_type": "int_or_null", "unit": None, "source": "selected initData event identity"},
  {"id": "controller_params_source_ordinal", "value_type": "int_or_null", "unit": None, "source": "selected initData event identity"},
  {"id": "controller_selection_source", "value_type": "enum", "unit": None, "source": "versioned controller selection state machine"},
  {"id": "controller_selection_valid", "value_type": "bool", "unit": None, "source": "versioned controller selection state machine"},
  {"id": "controller_type", "value_type": "enum", "unit": None, "source": "controlsd controller selected at initialization"},
  {"id": "controller_selection_stateful", "value_type": "bool", "unit": None, "source": "controller initialization provenance proof"},
  {"id": "controller_selection_state_machine_version", "value_type": "int", "unit": None, "source": "controller selection state machine"},
  {"id": "controller_selection_binding", "value_type": "enum", "unit": None, "source": "controller selection state machine"},
  {
    "id": "controller_selection_context_log_mono_time_ns",
    "value_type": "decimal_string",
    "unit": "ns",
    "source": "controlsState that established controller selection",
  },
  {
    "id": "controller_selection_observed_consistent",
    "value_type": "bool",
    "unit": None,
    "source": "later logged toggle observations versus initialized controller",
  },
  {"id": "controller_selection_evaluator_id", "value_type": "text_or_null", "unit": None, "source": "versioned initData fallback evaluator"},
  {
    "id": "controller_resolved_toggles_log_mono_time_ns",
    "value_type": "decimal_string_or_null",
    "unit": "ns",
    "source": "controller-selection toggle source identity",
  },
  {"id": "controller_resolved_toggles_segment_num", "value_type": "int_or_null", "unit": None, "source": "controller-selection toggle source identity"},
  {"id": "controller_resolved_toggles_source_ordinal", "value_type": "int_or_null", "unit": None, "source": "controller-selection toggle source identity"},
  {"id": "lateral_tune", "value_type": "bool_or_null", "unit": None, "source": "resolved controller selection context"},
  {"id": "lateral_tune_available", "value_type": "bool", "unit": None, "source": "resolved controller selection context"},
  {"id": "nnff_capable", "value_type": "bool_or_null", "unit": None, "source": "resolved controller selection context"},
  {"id": "nnff_capable_available", "value_type": "bool", "unit": None, "source": "resolved controller selection context"},
  {"id": "nnff_model_name", "value_type": "text_or_null", "unit": None, "source": "resolved controller selection context"},
  {"id": "nnff_model_name_available", "value_type": "bool", "unit": None, "source": "resolved controller selection context"},
  {"id": "effective_torque_params_missing_fields", "value_type": "list_text", "unit": None, "source": "stateful torque-context proof"},
  {"id": "effective_torque_params_source", "value_type": "object", "unit": None, "source": "owner of each effective torque parameter"},
  {"id": "effective_torque_params_source_identity", "value_type": "object", "unit": None, "source": "exact event identity for each effective torque parameter"},
  {"id": "effective_torque_params_source_age_us", "value_type": "object", "unit": "us", "source": "grid age of each effective torque parameter source"},
  {"id": "effective_torque_params_stateful", "value_type": "bool", "unit": None, "source": "controlsd torque-context state machine"},
  {"id": "effective_torque_params_state_machine_version", "value_type": "int", "unit": None, "source": "controlsd torque-context state machine"},
  {"id": "effective_torque_params_value_space", "value_type": "enum", "unit": None, "source": "controller profile contract"},
  {"id": "effective_torque_params_state_held", "value_type": "bool", "unit": None, "source": "controlsd torque-context state machine"},
  {"id": "effective_torque_state_origin", "value_type": "enum", "unit": None, "source": "controlsd torque-context state machine"},
  {
    "id": "effective_torque_params_last_update_log_mono_time_ns",
    "value_type": "decimal_string_or_null",
    "unit": "ns",
    "source": "last controlsState runtime parameter update",
  },
  {"id": "effective_torque_context_log_mono_time_ns", "value_type": "decimal_string", "unit": "ns", "source": "selected controlsState event"},
  {"id": "force_auto_tune", "value_type": "bool", "unit": None, "source": "resolved runtime toggles"},
  {"id": "force_auto_tune_off", "value_type": "bool", "unit": None, "source": "resolved runtime toggles"},
  {"id": "use_custom_lat_accel_factor", "value_type": "bool", "unit": None, "source": "resolved runtime toggles"},
  {"id": "use_custom_friction", "value_type": "bool", "unit": None, "source": "resolved runtime toggles"},
  {"id": "effective_resolved_toggles_sha256", "value_type": "text", "unit": None, "source": "effective torque-context toggle snapshot"},
  {"id": "effective_resolved_toggles_source", "value_type": "enum", "unit": None, "source": "effective torque-context toggle snapshot"},
  {"id": "effective_resolved_toggles_evaluator_id", "value_type": "text_or_null", "unit": None, "source": "versioned initData fallback evaluator"},
  {
    "id": "effective_resolved_toggles_log_mono_time_ns",
    "value_type": "decimal_string_or_null",
    "unit": "ns",
    "source": "effective torque-context toggle source identity",
  },
  {"id": "effective_resolved_toggles_segment_num", "value_type": "int_or_null", "unit": None, "source": "effective torque-context toggle source identity"},
  {"id": "effective_resolved_toggles_source_ordinal", "value_type": "int_or_null", "unit": None, "source": "effective torque-context toggle source identity"},
  {"id": "live_torque_replay_age_ok", "value_type": "bool", "unit": None, "source": "controlsState-bound liveTorqueParameters age check"},
  {"id": "live_torque_cadence_policy", "value_type": "enum", "unit": None, "source": "controlsState-bound liveTorqueParameters cadence evaluator"},
  {"id": "live_torque_cadence_policy_version", "value_type": "int", "unit": None, "source": "controlsState-bound liveTorqueParameters cadence evaluator"},
  {"id": "live_torque_used", "value_type": "bool", "unit": None, "source": "controlsd torque-context state machine"},
  {
    "id": "live_torque_source_log_mono_time_ns",
    "value_type": "decimal_string_or_null",
    "unit": "ns",
    "source": "controlsState-bound liveTorqueParameters event identity",
  },
  {"id": "live_torque_source_segment_num", "value_type": "int_or_null", "unit": None, "source": "controlsState-bound liveTorqueParameters event identity"},
  {"id": "live_torque_source_ordinal", "value_type": "int_or_null", "unit": None, "source": "controlsState-bound liveTorqueParameters event identity"},
  {"id": "baseline_controller_profile_id", "value_type": "text_or_null", "unit": None, "source": "reviewed profile selected by exact source commit"},
  {"id": "baseline_controller_params_sha256", "value_type": "text_or_null", "unit": None, "source": "reviewed profile canonical parameter hash"},
  {"id": "baseline_controller_source_starpilot_commit", "value_type": "text_or_null", "unit": None, "source": "reviewed profile exact source commit"},
  {"id": "vehicle_lat_accel_factor_multiplier", "value_type": "float_or_null", "unit": "1", "source": "reviewed controller profile"},
)

DYNAMICS_REQUIRED_FINITE_FIELDS = (
  "applied_torque",
  "actual_lateral_accel",
  "steering_angle_deg",
  "steering_rate_deg",
  "signed_steering_rate_deg_s",
  "steering_torque_eps",
  "v_ego",
  "a_ego",
  "desired_curvature",
  "desired_lateral_accel",
  "desired_lateral_jerk",
  "controller_output",
  "controller_i",
)


def _segment_identity(path: Path) -> tuple[str | None, int | None, str]:
  directory_name = path.parent.name if path.is_file() else path.name
  match = SEGMENT_RE.match(directory_name)
  if not match:
    return None, None, directory_name
  return match.group("route"), int(match.group("segment")), directory_name


def _candidate_log(segment_dir: Path, log_type: str) -> Path | None:
  for name in (log_type, f"{log_type}.zst", f"{log_type}.bz2"):
    candidate = segment_dir / name
    if candidate.is_file():
      return candidate
  return None


def discover_route(inputs: Sequence[Path], route_id: str | None = None, log_type: str = "rlog") -> RouteInput:
  if not inputs:
    raise InputError("at least one input is required")

  paths: list[Path] = []
  for raw_input in inputs:
    path = raw_input.expanduser().resolve()
    if not path.exists():
      raise InputError(f"path does not exist: {raw_input}")
    if path.is_file():
      paths.append(path)
      if len(paths) > MAX_ROUTE_SEGMENTS:
        raise ResourceLimitError(
          "route_segment_count",
          MAX_ROUTE_SEGMENTS,
          len(paths),
        )
      continue

    direct = _candidate_log(path, log_type)
    if direct is not None:
      paths.append(direct)
      if len(paths) > MAX_ROUTE_SEGMENTS:
        raise ResourceLimitError(
          "route_segment_count",
          MAX_ROUTE_SEGMENTS,
          len(paths),
        )
      continue

    for child in path.iterdir():
      if not child.is_dir():
        continue
      child_route, _, _ = _segment_identity(child)
      if route_id is not None and child_route != route_id:
        continue
      candidate = _candidate_log(child, log_type)
      if candidate is not None:
        paths.append(candidate)
        if len(paths) > MAX_ROUTE_SEGMENTS:
          raise ResourceLimitError(
            "route_segment_count",
            MAX_ROUTE_SEGMENTS,
            len(paths),
          )

  if not paths:
    selected = f" for route {route_id!r}" if route_id else ""
    raise InputError(f"no {log_type} logs found{selected}")

  by_segment: dict[tuple[str, int], SegmentInput] = {}
  discovered_routes: set[str] = set()
  unnumbered = 0
  for path in paths:
    discovered_route, segment_num, directory_name = _segment_identity(path)
    effective_route = route_id or discovered_route
    if effective_route is None:
      if len(paths) != 1:
        raise InputError(f"cannot infer route and segment from {path.parent.name!r}")
      effective_route = route_id or path.parent.name
      segment_num = 0
    if route_id is not None and discovered_route is not None and discovered_route != route_id:
      continue
    if segment_num is None:
      segment_num = unnumbered
      unnumbered += 1
    discovered_routes.add(effective_route)
    key = (effective_route, segment_num)
    existing = by_segment.get(key)
    if existing is None or _log_preference(path, log_type) < _log_preference(existing.path, log_type):
      by_segment[key] = SegmentInput(segment_num, path, directory_name)

  if not by_segment:
    raise InputError(f"no logs matched route {route_id!r}")
  if route_id is None and len(discovered_routes) != 1:
    choices = ", ".join(sorted(discovered_routes))
    raise InputError(f"input contains multiple routes ({choices}); pass --route-id")

  selected_route = route_id or next(iter(discovered_routes))
  segments = tuple(segment for (candidate_route, _), segment in sorted(by_segment.items(), key=lambda item: item[0][1]) if candidate_route == selected_route)
  if not segments:
    raise InputError(f"no segments found for route {selected_route!r}")
  if len(segments) > MAX_ROUTE_SEGMENTS:
    raise ResourceLimitError(
      "route_segment_count",
      MAX_ROUTE_SEGMENTS,
      len(segments),
    )
  return RouteInput(selected_route, log_type, segments)


def _log_preference(path: Path, log_type: str) -> int:
  order = {log_type: 0, f"{log_type}.zst": 1, f"{log_type}.bz2": 2}
  return order.get(path.name, 99)


def _finite(value: Any) -> float | None:
  try:
    result = float(value)
  except Exception:
    return None
  return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
  try:
    return int(value)
  except Exception:
    return None


def _boolean(value: Any) -> bool | None:
  try:
    return bool(value)
  except Exception:
    return None


def _text(value: Any) -> str | None:
  try:
    return str(value)
  except Exception:
    return None


def _get(obj: Any, *names: str) -> Any | None:
  current = obj
  for name in names:
    if current is None:
      return None
    try:
      current = getattr(current, name)
    except Exception:
      return None
  return current


def _float_list(value: Any, limit: int = 256) -> list[float]:
  if value is None:
    return []
  result: list[float] = []
  try:
    for item in value:
      number = _finite(item)
      if number is not None:
        result.append(number)
      if len(result) >= limit:
        break
  except Exception:
    return []
  return result


def _mean(values: Iterable[Any]) -> float | None:
  finite_values = [value for item in values if (value := _finite(item)) is not None]
  return sum(finite_values) / len(finite_values) if finite_values else None


def _maximum(values: Iterable[Any]) -> float | None:
  finite_values = [value for item in values if (value := _finite(item)) is not None]
  return max(finite_values) if finite_values else None


def _compression_name(path: Path, prefix: bytes) -> str:
  if path.suffix.lower() == ".zst" or prefix.startswith(ZSTD_MAGIC):
    return "zstd"
  if path.suffix.lower() == ".bz2" or prefix.startswith(BZ2_MAGIC):
    return "bzip2"
  return "none"


def _bounded_stream_bytes(
  stream: Any,
  *,
  limit: int,
  code: str,
) -> bytes:
  payload = bytearray()
  while True:
    chunk = stream.read(READ_CHUNK_BYTES)
    if not chunk:
      break
    observed = len(payload) + len(chunk)
    if observed > limit:
      raise ResourceLimitError(code, limit, observed)
    payload.extend(chunk)
  return bytes(payload)


def _read_log_details(
  path: Path,
  *,
  max_source_bytes: int = MAX_SOURCE_BYTES_PER_SEGMENT,
  max_decompressed_bytes: int = MAX_DECOMPRESSED_BYTES_PER_SEGMENT,
) -> tuple[bytes, str, str, int]:
  digest = hashlib.sha256()
  with path.open("rb") as source:
    prefix = source.read(max(len(ZSTD_MAGIC), len(BZ2_MAGIC)))
    digest.update(prefix)
    source_size = len(prefix)
    if source_size > max_source_bytes:
      raise ResourceLimitError(
        "segment_source_bytes",
        max_source_bytes,
        source_size,
      )
    while True:
      chunk = source.read(READ_CHUNK_BYTES)
      if not chunk:
        break
      source_size += len(chunk)
      if source_size > max_source_bytes:
        raise ResourceLimitError(
          "segment_source_bytes",
          max_source_bytes,
          source_size,
        )
      digest.update(chunk)

    compression = _compression_name(path, prefix)
    source.seek(0)
    if compression == "zstd":
      with zstd.ZstdDecompressor().stream_reader(
        source,
        closefd=False,
      ) as reader:
        payload = _bounded_stream_bytes(
          reader,
          limit=max_decompressed_bytes,
          code="segment_decompressed_bytes",
        )
    elif compression == "bzip2":
      with bz2.BZ2File(source, "rb") as reader:
        payload = _bounded_stream_bytes(
          reader,
          limit=max_decompressed_bytes,
          code="segment_decompressed_bytes",
        )
    else:
      payload = _bounded_stream_bytes(
        source,
        limit=max_decompressed_bytes,
        code="segment_decompressed_bytes",
      )
  return (
    payload,
    digest.hexdigest(),
    compression,
    source_size,
  )


def _read_log(
  path: Path,
  *,
  max_source_bytes: int = MAX_SOURCE_BYTES_PER_SEGMENT,
  max_decompressed_bytes: int = MAX_DECOMPRESSED_BYTES_PER_SEGMENT,
) -> tuple[bytes, str, str]:
  payload, digest, compression, _ = _read_log_details(
    path,
    max_source_bytes=max_source_bytes,
    max_decompressed_bytes=max_decompressed_bytes,
  )
  return payload, digest, compression


def _validated_capnp_prefix(
  payload: bytes,
  *,
  max_messages: int,
  max_event_bytes: int,
) -> tuple[int, int, bool]:
  offset = 0
  message_count = 0
  payload_size = len(payload)
  while offset < payload_size:
    remaining = payload_size - offset
    if remaining < 8:
      return offset, message_count, True
    segment_count = struct.unpack_from("<I", payload, offset)[0] + 1
    if segment_count > MAX_CAPNP_SEGMENTS_PER_MESSAGE:
      raise ResourceLimitError(
        "event_capnp_segment_count",
        MAX_CAPNP_SEGMENTS_PER_MESSAGE,
        segment_count,
      )
    table_unpadded_bytes = 4 * (segment_count + 1)
    table_bytes = (table_unpadded_bytes + 7) & ~7
    if remaining < table_bytes:
      return offset, message_count, True
    segment_words = struct.unpack_from(
      f"<{segment_count}I",
      payload,
      offset + 4,
    )
    body_bytes = sum(segment_words) * 8
    event_bytes = table_bytes + body_bytes
    if event_bytes > max_event_bytes:
      raise ResourceLimitError(
        "event_bytes",
        max_event_bytes,
        event_bytes,
      )
    if event_bytes > remaining:
      return offset, message_count, True
    message_count += 1
    if message_count > max_messages:
      raise ResourceLimitError(
        "segment_message_count",
        max_messages,
        message_count,
      )
    offset += event_bytes
  return offset, message_count, False


def _read_events(
  payload: bytes,
  *,
  max_messages: int = MAX_MESSAGES_PER_SEGMENT,
  max_event_bytes: int = MAX_EVENT_BYTES,
) -> tuple[list[Any], bool]:
  valid_end, expected_count, corrupt = _validated_capnp_prefix(
    payload,
    max_messages=max_messages,
    max_event_bytes=max_event_bytes,
  )
  events: list[Any] = []
  if valid_end == 0:
    return events, corrupt
  try:
    for event in capnp_log.Event.read_multiple_bytes(
      memoryview(payload)[:valid_end],
    ):
      events.append(event)
  except capnp.KjException:
    corrupt = True
  if len(events) != expected_count:
    corrupt = True
  return events, corrupt


def _timeline_start_ns(events: Sequence[Any]) -> int | None:
  camera_times: list[int] = []
  event_times: list[int] = []
  for event in events:
    if not _event_valid(event):
      continue
    which = _event_which(event)
    mono = _integer(_get(event, "logMonoTime"))
    if which in CAMERA_SERVICES:
      try:
        index = getattr(event, which)
        camera_time = _integer(_get(index, "timestampSof")) or _integer(_get(index, "timestampEof"))
        if camera_time is not None and camera_time > 0:
          camera_times.append(camera_time)
      except Exception:
        pass
    # initData retains manager-start time and can predate a later drive by hours.
    if which != "initData" and mono is not None and mono > 0:
      event_times.append(mono)
  if camera_times:
    return min(camera_times)
  return min(event_times) if event_times else None


def _schema_sha256() -> str | None:
  repo = Path(__file__).resolve().parents[4]
  schema_paths = (repo / "cereal/log.capnp", repo / "cereal/car.capnp", repo / "cereal/custom.capnp")
  if not all(path.is_file() for path in schema_paths):
    return None
  digest = hashlib.sha256()
  for path in schema_paths:
    digest.update(path.name.encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
  return digest.hexdigest()


def _starpilot_commit() -> str | None:
  configured = os.getenv("STARPILOT_COMMIT")
  if configured:
    return configured
  repo = Path(__file__).resolve().parents[4]
  try:
    return subprocess.run(
      ["git", "rev-parse", "HEAD"],
      cwd=repo,
      capture_output=True,
      check=True,
      text=True,
      timeout=3,
    ).stdout.strip()
  except Exception:
    return None


def _extractor_source_sha256() -> str:
  package = Path(__file__).resolve().parent
  digest = hashlib.sha256()
  for path in sorted(package.glob("*.py")):
    digest.update(path.name.encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
  return digest.hexdigest()


def _extractor_dirty() -> bool | None:
  repo = Path(__file__).resolve().parents[4]
  package = Path(__file__).resolve().parent
  try:
    relative = package.relative_to(repo)
    status = subprocess.run(
      ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", str(relative)],
      cwd=repo,
      capture_output=True,
      check=True,
      text=True,
      timeout=3,
    ).stdout
    return bool(status.strip())
  except Exception:
    return None


def _t_us(mono_ns: int, origin_ns: int) -> int:
  return (mono_ns - origin_ns) // 1_000


class _SignalChunker:
  def __init__(self, chunk_size: int):
    self.chunk_size = chunk_size
    self.buffers: dict[
      tuple[str, str],
      tuple[list[int], list[Any], list[str], list[int], list[int]],
    ] = {}
    self.chunk_numbers: Counter[tuple[str, str]] = Counter()
    self.sample_counts: Counter[str] = Counter()
    self.ranges: dict[str, list[int]] = {}

  def add(
    self,
    signal: str,
    tier: str,
    t_us: int,
    value: Any,
    log_mono_time_ns: int,
    segment_num: int,
    source_ordinal: int,
  ) -> list[dict[str, Any]]:
    if value is None:
      return []
    if isinstance(value, float) and not math.isfinite(value):
      return []
    key = (signal, tier)
    times, values, mono_times, segments, ordinals = self.buffers.setdefault(
      key,
      ([], [], [], [], []),
    )
    times.append(t_us)
    values.append(value)
    mono_times.append(str(log_mono_time_ns))
    segments.append(segment_num)
    ordinals.append(source_ordinal)
    if tier == "full":
      self.sample_counts[signal] += 1
      bounds = self.ranges.setdefault(signal, [t_us, t_us])
      bounds[1] = t_us
    if len(times) >= self.chunk_size:
      return [self._flush_key(key)]
    return []

  def _flush_key(self, key: tuple[str, str]) -> dict[str, Any]:
    signal, tier = key
    times, values, mono_times, segments, ordinals = self.buffers[key]
    chunk = self.chunk_numbers[key]
    self.chunk_numbers[key] += 1
    record = {
      "record": "series_chunk",
      "signal": signal,
      "tier": tier,
      "chunk": chunk,
      "t_us": times,
      "v": values,
      "log_mono_time_ns": mono_times,
      "source_segment_num": segments,
      "source_ordinal": ordinals,
    }
    self.buffers[key] = ([], [], [], [], [])
    return record

  def flush(self) -> Iterator[dict[str, Any]]:
    for key in sorted(self.buffers):
      if self.buffers[key][0]:
        yield self._flush_key(key)


class _Bucket:
  def __init__(
    self,
    t_us: int,
    value: Any,
    ordinal: int,
    log_mono_time_ns: int,
    segment_num: int,
    source_ordinal: int,
  ):
    self.samples: list[tuple[int, Any, int, int, int, int]] = [
      (t_us, value, ordinal, log_mono_time_ns, segment_num, source_ordinal),
    ]

  def add(
    self,
    t_us: int,
    value: Any,
    ordinal: int,
    log_mono_time_ns: int,
    segment_num: int,
    source_ordinal: int,
  ) -> None:
    self.samples.append(
      (t_us, value, ordinal, log_mono_time_ns, segment_num, source_ordinal),
    )

  def selected(self, value_type: str) -> list[tuple[int, Any, int, int, int]]:
    if not self.samples:
      return []
    selected = {0, len(self.samples) - 1}
    if value_type == "float":
      finite = [(index, sample) for index, sample in enumerate(self.samples) if isinstance(sample[1], (int, float)) and math.isfinite(float(sample[1]))]
      if finite:
        selected.add(min(finite, key=lambda item: (float(item[1][1]), item[1][0], item[1][2]))[0])
        selected.add(max(finite, key=lambda item: (float(item[1][1]), -item[1][0], -item[1][2]))[0])
    else:
      previous = self.samples[0][1]
      for index, sample in enumerate(self.samples[1:], start=1):
        if sample[1] != previous:
          selected.add(index - 1)
          selected.add(index)
        previous = sample[1]
    return [
      (
        self.samples[index][0],
        self.samples[index][1],
        self.samples[index][3],
        self.samples[index][4],
        self.samples[index][5],
      )
      for index in sorted(selected)
    ]


class _Downsampler:
  def __init__(self):
    self.buckets: dict[tuple[str, int], tuple[int, _Bucket]] = {}
    self.ordinal = 0

  def add(
    self,
    signal: str,
    value_type: str,
    t_us: int,
    value: Any,
    log_mono_time_ns: int,
    segment_num: int,
    source_ordinal: int,
  ) -> list[tuple[str, str, int, Any, int, int, int]]:
    self.ordinal += 1
    emitted: list[tuple[str, str, int, Any, int, int, int]] = []
    for width in TIER_WIDTHS_US:
      key = (signal, width)
      bucket_number = t_us // width
      existing = self.buckets.get(key)
      if existing is None:
        self.buckets[key] = (
          bucket_number,
          _Bucket(
            t_us,
            value,
            self.ordinal,
            log_mono_time_ns,
            segment_num,
            source_ordinal,
          ),
        )
      elif existing[0] == bucket_number:
        existing[1].add(
          t_us,
          value,
          self.ordinal,
          log_mono_time_ns,
          segment_num,
          source_ordinal,
        )
      else:
        tier = _tier_name(width)
        emitted.extend(
          (signal, tier, sample_t, sample_v, sample_mono, sample_segment, sample_ordinal)
          for sample_t, sample_v, sample_mono, sample_segment, sample_ordinal in existing[1].selected(value_type)
        )
        self.buckets[key] = (
          bucket_number,
          _Bucket(
            t_us,
            value,
            self.ordinal,
            log_mono_time_ns,
            segment_num,
            source_ordinal,
          ),
        )
    return emitted

  def flush(self) -> list[tuple[str, str, int, Any, int, int, int]]:
    emitted: list[tuple[str, str, int, Any, int, int, int]] = []
    for (signal, width), (_, bucket) in sorted(self.buckets.items()):
      spec = SIGNAL_BY_ID[signal]
      emitted.extend(
        (signal, _tier_name(width), t_us, value, mono, segment, ordinal) for t_us, value, mono, segment, ordinal in bucket.selected(spec.value_type)
      )
    self.buckets.clear()
    return emitted


def _tier_name(width_us: int) -> str:
  return {
    100_000: "100ms",
    500_000: "500ms",
    2_000_000: "2s",
    10_000_000: "10s",
  }[width_us]


class _RecordChunker:
  def __init__(self, record_name: str, group_key: str, chunk_size: int):
    self.record_name = record_name
    self.group_key = group_key
    self.chunk_size = chunk_size
    self.buffers: dict[str, list[dict[str, Any]]] = {}
    self.chunk_numbers: Counter[str] = Counter()

  def add(self, group: str, row: dict[str, Any]) -> list[dict[str, Any]]:
    buffer = self.buffers.setdefault(group, [])
    buffer.append(row)
    if len(buffer) >= self.chunk_size:
      return [self._flush_group(group)]
    return []

  def _flush_group(self, group: str) -> dict[str, Any]:
    rows = self.buffers[group]
    chunk = self.chunk_numbers[group]
    self.chunk_numbers[group] += 1
    self.buffers[group] = []
    return {
      "record": self.record_name,
      self.group_key: group,
      "chunk": chunk,
      "rows": rows,
    }

  def flush_group(
    self,
    group: str,
  ) -> list[dict[str, Any]]:
    if not self.buffers.get(group):
      return []
    return [self._flush_group(group)]

  def flush(self) -> Iterator[dict[str, Any]]:
    for group in sorted(self.buffers):
      if self.buffers[group]:
        yield self._flush_group(group)


class _CausalServiceHistory:
  def __init__(self) -> None:
    self.times: dict[str, list[int]] = {}
    self.values: dict[str, list[dict[str, Any]]] = {}

  def add(self, service: str, snapshot: dict[str, Any]) -> None:
    mono_ns = int(snapshot["_mono_ns"])
    times = self.times.setdefault(service, [])
    values = self.values.setdefault(service, [])
    if times and mono_ns < times[-1]:
      raise ExtractionError(
        "causal service history must be added in monotonic timestamp order",
      )
    snapshot["_previous_mono_ns"] = times[-1] if times else None
    times.append(mono_ns)
    values.append(snapshot)

  def asof(
    self,
    service: str,
    mono_ns: int,
  ) -> dict[str, Any] | None:
    times = self.times.get(service)
    if not times:
      return None
    position = bisect_right(times, mono_ns) - 1
    if position < 0:
      return None
    return self.values[service][position]

  def snapshots_asof(self, mono_ns: int) -> dict[str, dict[str, Any]]:
    return {
      service: snapshot
      for service in (
        "carState",
        "carControl",
        "carOutput",
        "controlsState",
        "liveTorqueParameters",
        "liveParameters",
        "starpilotPlan",
      )
      if (snapshot := self.asof(service, mono_ns)) is not None
    }

  def clear(self) -> None:
    self.times.clear()
    self.values.clear()


class _IntervalMarkers:
  def __init__(self):
    self.open: dict[str, tuple[int, dict[str, Any]]] = {}
    self.counter = 0

  def transition(
    self,
    key: str,
    active: bool,
    t_us: int,
    *,
    kind: str,
    label: str,
    severity: str = "info",
    attributes: dict[str, Any] | None = None,
  ) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if active and key not in self.open:
      self.open[key] = (
        t_us,
        {
          "kind": kind,
          "label": label,
          "severity": severity,
          "attributes": attributes or {},
        },
      )
    elif not active and key in self.open:
      start_us, metadata = self.open.pop(key)
      records.append(self._record(start_us, t_us, metadata))
    return records

  def point(
    self,
    t_us: int,
    *,
    kind: str,
    label: str,
    severity: str = "info",
    attributes: dict[str, Any] | None = None,
  ) -> dict[str, Any]:
    return self._record(
      t_us,
      t_us,
      {
        "kind": kind,
        "label": label,
        "severity": severity,
        "attributes": attributes or {},
      },
    )

  def close_all(self, end_us: int) -> list[dict[str, Any]]:
    records = [self._record(start, end_us, metadata) for start, metadata in self.open.values()]
    self.open.clear()
    return records

  def _record(self, start_us: int, end_us: int, metadata: dict[str, Any]) -> dict[str, Any]:
    marker_id = f"m{self.counter:08d}"
    self.counter += 1
    return {
      "record": "marker",
      "id": marker_id,
      "start_us": start_us,
      "end_us": max(start_us, end_us),
      **metadata,
    }


def _event_which(event: Any) -> str | None:
  try:
    return event.which()
  except Exception:
    return None


def _event_fields(event: Any, which: str) -> dict[str, Any]:
  if which not in SCALAR_SERVICES:
    return {}
  data = getattr(event, which)
  if which == "carState":
    cruise = _get(data, "cruiseState")
    return {
      "vehicle.speed": _finite(_get(data, "vEgo")),
      "vehicle.acceleration": _finite(_get(data, "aEgo")),
      "vehicle.speed_raw": _finite(_get(data, "vEgoRaw")),
      "vehicle.yaw_rate": _finite(_get(data, "yawRate")),
      "vehicle.steering_angle": _radians(_get(data, "steeringAngleDeg")),
      "vehicle.steering_rate_abs": _radians_abs(_get(data, "steeringRateDeg")),
      "vehicle.steering_torque_driver": _finite(_get(data, "steeringTorque")),
      "vehicle.steering_torque_eps": _finite(_get(data, "steeringTorqueEps")),
      "vehicle.steering_pressed": _boolean(_get(data, "steeringPressed")),
      "vehicle.steering_disengage": _boolean(_get(data, "steeringDisengage")),
      "vehicle.steer_fault_temporary": _boolean(_get(data, "steerFaultTemporary")),
      "vehicle.steer_fault_permanent": _boolean(_get(data, "steerFaultPermanent")),
      "vehicle.gas_pressed": _boolean(_get(data, "gasPressed")),
      "vehicle.brake_pressed": _boolean(_get(data, "brakePressed")),
      "vehicle.standstill": _boolean(_get(data, "standstill")),
      "vehicle.cruise_enabled": _boolean(_get(cruise, "enabled")),
      "vehicle.cruise_set_speed": _finite(_get(cruise, "speed")),
      "vehicle.gear": _text(_get(data, "gearShifter")),
      "vehicle.fuel_or_charge_fraction": _finite(_get(data, "fuelGauge")),
    }
  if which == "carControl":
    actuators = _get(data, "actuators")
    return {
      "control.enabled": _boolean(_get(data, "enabled")),
      "control.lateral_active": _boolean(_get(data, "latActive")),
      "control.longitudinal_active": _boolean(_get(data, "longActive")),
      "control.torque_command": _finite(_get(actuators, "torque")),
      "control.steering_angle_command": _radians(_get(actuators, "steeringAngleDeg")),
      "control.curvature_command": _finite(_get(actuators, "curvature")),
      "control.acceleration_command": _finite(_get(actuators, "accel")),
      "control.torque_output_can": _finite(_get(actuators, "torqueOutputCan")),
      "control.current_curvature": _finite(_get(data, "currentCurvature")),
    }
  if which == "carOutput":
    actuators = _get(data, "actuatorsOutput")
    return {
      "control.applied_torque": _finite(_get(actuators, "torque")),
      "control.applied_steering_angle": _radians(_get(actuators, "steeringAngleDeg")),
      "control.applied_curvature": _finite(_get(actuators, "curvature")),
      "control.applied_acceleration": _finite(_get(actuators, "accel")),
    }
  if which == "controlsState":
    torque_state = None
    lateral = _get(data, "lateralControlState")
    try:
      if lateral is not None and lateral.which() == "torqueState":
        torque_state = lateral.torqueState
    except Exception:
      torque_state = None
    result = {
      "lateral.desired_curvature": _finite(_get(data, "desiredCurvature")),
      "lateral.measured_curvature": _finite(_get(data, "curvature")),
    }
    if torque_state is not None:
      result.update(
        {
          "lateral.active": _boolean(_get(torque_state, "active")),
          "lateral.actual_acceleration": _finite(_get(torque_state, "actualLateralAccel")),
          "lateral.desired_acceleration": _finite(_get(torque_state, "desiredLateralAccel")),
          "lateral.desired_jerk": _finite(_get(torque_state, "desiredLateralJerk")),
          "lateral.error": _finite(_get(torque_state, "error")),
          "lateral.error_rate": _finite(_get(torque_state, "errorRate")),
          "lateral.p": _finite(_get(torque_state, "p")),
          "lateral.i": _finite(_get(torque_state, "i")),
          "lateral.d": _finite(_get(torque_state, "d")),
          "lateral.f": _finite(_get(torque_state, "f")),
          "lateral.output": _finite(_get(torque_state, "output")),
          "lateral.saturated": _boolean(_get(torque_state, "saturated")),
        }
      )
    return result
  if which == "liveTorqueParameters":
    return {
      "live_tune.valid": _boolean(_get(data, "liveValid")),
      "live_tune.in_use": _boolean(_get(data, "useParams")),
      "live_tune.lateral_accel_factor": _finite(_get(data, "latAccelFactorFiltered")),
      "live_tune.lateral_accel_offset": _finite(_get(data, "latAccelOffsetFiltered")),
      "live_tune.friction": _finite(_get(data, "frictionCoefficientFiltered")),
    }
  if which == "liveParameters":
    return {
      "live_tune.steer_ratio": _finite(_get(data, "steerRatio")),
      "live_tune.stiffness_factor": _finite(_get(data, "stiffnessFactor")),
      "live_tune.roll": _finite(_get(data, "roll")),
    }
  if which == "modelV2":
    return {
      "model.desired_curvature": _finite(_get(data, "action", "desiredCurvature")),
      "model.desired_acceleration": _finite(_get(data, "action", "desiredAcceleration")),
    }
  if which == "selfdriveState":
    return {
      "selfdrive.enabled": _boolean(_get(data, "enabled")),
      "selfdrive.active": _boolean(_get(data, "active")),
      "selfdrive.engageable": _boolean(_get(data, "engageable")),
      "selfdrive.state": _text(_get(data, "state")),
    }
  if which == "deviceState":
    cpu_usage = _get(data, "cpuUsagePercent")
    return {
      "device.onroad": _boolean(_get(data, "started")),
      "device.storage_free": _percent_fraction(_get(data, "freeSpacePercent")),
      "device.memory_used": _percent_fraction(_get(data, "memoryUsagePercent")),
      "device.cpu_used_mean": _percent_fraction(_mean(cpu_usage or [])),
      "device.cpu_temperature_max": _maximum(_get(data, "cpuTempC") or []),
      "device.gpu_temperature_max": _maximum(_get(data, "gpuTempC") or []),
      "device.power_draw": _finite(_get(data, "powerDrawW")),
      "device.network_type": _text(_get(data, "networkType")),
      "device.network_strength": _text(_get(data, "networkStrength")),
      "device.network_metered": _boolean(_get(data, "networkMetered")),
    }
  if which in ("gpsLocation", "gpsLocationExternal"):
    prefix = "gps_external" if which == "gpsLocationExternal" else "gps"
    return {
      f"{prefix}.latitude": _finite(_get(data, "latitude")),
      f"{prefix}.longitude": _finite(_get(data, "longitude")),
      f"{prefix}.altitude": _finite(_get(data, "altitude")),
      f"{prefix}.speed": _finite(_get(data, "speed")),
      f"{prefix}.bearing": _finite(_get(data, "bearingDeg")),
      f"{prefix}.horizontal_accuracy": _finite(_get(data, "horizontalAccuracy")),
      f"{prefix}.has_fix": _boolean(_get(data, "hasFix")),
    }
  return {}


def _radians(value: Any) -> float | None:
  number = _finite(value)
  return math.radians(number) if number is not None else None


def _radians_abs(value: Any) -> float | None:
  number = _finite(value)
  return math.radians(abs(number)) if number is not None else None


def _percent_fraction(value: Any) -> float | None:
  number = _finite(value)
  return number / 100.0 if number is not None else None


def _car_params_snapshot(event: Any, segment_num: int, t_us: int) -> dict[str, Any]:
  data = event.carParams
  lateral_tuning_type = None
  torque_tune = None
  try:
    lateral_tuning_type = data.lateralTuning.which()
    if lateral_tuning_type == "torque":
      torque_tune = data.lateralTuning.torque
  except Exception:
    pass
  return {
    "source_segment": segment_num,
    "t_us": t_us,
    "brand": _text(_get(data, "brand")) or "",
    "car_fingerprint": _text(_get(data, "carFingerprint")) or "",
    "not_car": _boolean(_get(data, "notCar")),
    "openpilot_longitudinal_control": _boolean(_get(data, "openpilotLongitudinalControl")),
    "pcm_cruise": _boolean(_get(data, "pcmCruise")),
    "steer_control_type": _text(_get(data, "steerControlType")),
    "wheelbase_m": _finite(_get(data, "wheelbase")),
    "steer_ratio": _finite(_get(data, "steerRatio")),
    "mass_kg": _finite(_get(data, "mass")),
    "min_steer_speed_mps": _finite(_get(data, "minSteerSpeed")),
    "lateral_tuning_type": lateral_tuning_type,
    "lateral_torque_tuning": {
      "lat_accel_factor": _finite(_get(torque_tune, "latAccelFactor")),
      "lat_accel_offset": _finite(_get(torque_tune, "latAccelOffset")),
      "friction": _finite(_get(torque_tune, "friction")),
      "steering_angle_deadzone_deg": _finite(_get(torque_tune, "steeringAngleDeadzoneDeg")),
    }
    if torque_tune is not None
    else None,
  }


def _struct_wire_sha256(value: Any) -> str | None:
  try:
    return hashlib.sha256(value.as_builder().to_bytes()).hexdigest()
  except Exception:
    return None


def _flm_overrides_runtime_active(raw: bytes) -> tuple[bool | None, bool]:
  try:
    decoded = raw.decode("utf-8").strip()
    value = json.loads(decoded) if decoded else {}
  except (UnicodeDecodeError, json.JSONDecodeError):
    return None, False
  if not isinstance(value, dict):
    return False, True
  vehicle_knobs = value.get("vehicleKnobs", {})
  if isinstance(vehicle_knobs, dict):
    for knob_value in vehicle_knobs.values():
      if _finite(knob_value) is not None:
        return True, True
  thresholds = value.get("baseFrictionThresholds", {})
  if isinstance(thresholds, dict):
    for family in ("gm", "standard", "hkg_canfd"):
      payload = thresholds.get(family, {})
      values = payload.get("values", []) if isinstance(payload, dict) else payload
      if isinstance(values, (list, tuple)) and len(values) == 5 and all(_finite(item) is not None for item in values):
        return True, True
  return False, True


def _controller_params_snapshot(data: Any) -> dict[str, Any]:
  snapshot: dict[str, Any] = {}
  entries = _get(data, "params", "entries")
  if entries is None:
    return snapshot
  for entry in entries:
    key = _text(_get(entry, "key"))
    if key not in CONTROLLER_PARAM_ALLOWLIST:
      continue
    try:
      raw = bytes(entry.value)
    except Exception:
      continue
    item: dict[str, Any] = {
      "sha256": hashlib.sha256(raw).hexdigest(),
      "size_bytes": len(raw),
      "value_base64": base64.b64encode(raw).decode("ascii"),
    }
    if key == "FLMActiveOverrides":
      runtime_active, valid = _flm_overrides_runtime_active(raw)
      item["runtime_active"] = runtime_active
      item["valid"] = valid
    try:
      decoded = raw.decode("utf-8")
      if len(decoded) <= 2048 and all(character.isprintable() or character in "\r\n\t" for character in decoded):
        item["text"] = decoded
    except UnicodeDecodeError:
      pass
    snapshot[key] = item
  return dict(sorted(snapshot.items()))


def _init_data_snapshot(event: Any, segment_num: int, t_us: int) -> dict[str, Any]:
  data = event.initData
  controller_params = _controller_params_snapshot(data)
  controller_params_sha256 = hashlib.sha256(json.dumps(controller_params, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
  return {
    "source_segment": segment_num,
    "t_us": t_us,
    "device_type": _text(_get(data, "deviceType")),
    "version": _text(_get(data, "version")),
    "git_commit": _text(_get(data, "gitCommit")),
    "git_source_commit": _text(_get(data, "gitSrcCommit")),
    "git_branch": _text(_get(data, "gitBranch")),
    "git_remote": _text(_get(data, "gitRemote")),
    "dirty": _boolean(_get(data, "dirty")),
    "passive": _boolean(_get(data, "passive")),
    "wall_time_ns": str(_integer(_get(data, "wallTimeNanos")) or 0),
    "controller_params": controller_params,
    "controller_params_sha256": controller_params_sha256,
  }


def _param_text(
  params: dict[str, Any],
  key: str,
) -> str | None:
  item = params.get(key)
  if not isinstance(item, dict) or "text" not in item:
    return None
  return str(item["text"])


def _param_bool(
  params: dict[str, Any],
  key: str,
) -> bool | None:
  text = _param_text(params, key)
  if text is None:
    return None
  normalized = text.strip().lower()
  if normalized in ("1", "true"):
    return True
  if normalized in ("0", "false", ""):
    return False
  return None


def _param_float(
  params: dict[str, Any],
  key: str,
) -> float | None:
  return _finite(_param_text(params, key))


def _param_bool_default_false(
  params: dict[str, Any],
  key: str,
) -> bool:
  return bool(_param_bool(params, key))


def _clamp(value: float, low: float, high: float) -> float:
  return max(low, min(high, value))


KNOWN_TORQUE_CONTEXT_EVALUATORS = {
  "2747bf037c0f284500457f1befb4f52415e3285a": ("starpilot-torque-context-2747bf-v1"),
  "6dd6c0a3d558842b91b903e1cddfaca576a69c25": ("starpilot-torque-context-6dd6c0-v1"),
}
HISTORICAL_IONIQ5_CONTROLLER_PROFILE_ID = "starpilot-ioniq5-torque-2747bf037c0f-v1"
HISTORICAL_IONIQ5_CONTROLLER_PARAMS = {
  "base_lat_accel_factor_mult": 1.2101,
  "ff_reduction_left": 0.12,
  "ff_reduction_right": 0.22,
  "turn_in_boost_left": 0.14,
  "turn_in_boost_right": 0.06,
  "unwind_taper_left": 0.76,
  "unwind_taper_right": 0.85,
  "turn_in_threshold_reduction_left": 0.08,
  "turn_in_threshold_reduction_right": 0.05,
  "unwind_threshold_increase_left": 0.36,
  "unwind_threshold_increase_right": 0.38,
  "turn_in_friction_boost_left": 0.04,
  "turn_in_friction_boost_right": 0.03,
  "unwind_friction_reduction_left": 0.34,
  "unwind_friction_reduction_right": 0.34,
  "friction_scale_mult": 0.729,
  "center_taper_max": 0.24,
  "center_taper_lat": 0.12,
  "center_taper_lat_width": 0.03,
  "center_taper_speed": 16.0,
  "center_taper_speed_width": 2.5,
  "sustained_turn_in_ff_boost_left": 0.0,
  "sustained_turn_in_ff_boost_right": 0.0,
  "sustained_turn_in_ff_speed": 13.5,
  "sustained_turn_in_ff_speed_width": 1.8,
  "sustained_turn_in_ff_lat_start": 1.1,
  "sustained_turn_in_ff_lat_end": 3.6,
  "sustained_turn_in_ff_lat_width": 0.3,
  "steady_high_lat_taper": 0.0,
  "steady_high_lat_start": 0.35,
  "steady_high_lat_width": 0.12,
  "steady_jerk_width": 0.08,
  "hkg_friction_threshold": False,
  "damping_gain": 0.0,
  "turn_exit_damping_gain": 0.0,
  "turn_exit_damping_gain_right": 0.0,
  "reversal_damping_gain": 0.0,
  "reversal_hold_seconds": 0.6,
  "steering_rate_feedback_gain": 0.0,
}
HISTORICAL_IONIQ5_CONTROLLER_PARAMS_SHA256 = "f8dc55e57772cd4850e37db43e494b4103384acc325627b0674a793952dd7a12"
REFINED_IONIQ5_CONTROLLER_SOURCE_COMMIT = "6dd6c0a3d558842b91b903e1cddfaca576a69c25"
REFINED_IONIQ5_CONTROLLER_PROFILE_ID = "starpilot-ioniq5-torque-6dd6c0a3d558-v1"
REFINED_IONIQ5_CONTROLLER_PARAMS = {
  **HISTORICAL_IONIQ5_CONTROLLER_PARAMS,
  "base_lat_accel_factor_mult": 1.2507,
  "turn_in_boost_left": 0.1761,
  "unwind_taper_right": 0.8885,
  "friction_scale_mult": 1.0,
  "center_taper_max": 0.2412,
}
REFINED_IONIQ5_CONTROLLER_PARAMS_SHA256 = "f02fff8adef34f524607bc0f200ad486523850a2ec576c36d664d9b369175ce8"
CURRENT_IONIQ5_CONTROLLER_SOURCE_COMMIT = "19f8c767ec0d3b6fc1000aa1effbeec625ddd753"
CURRENT_IONIQ5_CONTROLLER_PROFILE_ID = "starpilot-ioniq5-torque-19f8c767ec0d-v1"
CURRENT_IONIQ5_CONTROLLER_PARAMS = {
  "base_lat_accel_factor_mult": 1.36,
  "ff_reduction_left": 0.2625,
  "ff_reduction_right": 0.415,
  "turn_in_boost_left": 0.135,
  "turn_in_boost_right": 0.02,
  "unwind_taper_left": 1.15,
  "unwind_taper_right": 1.39,
  "turn_in_threshold_reduction_left": 0.125,
  "turn_in_threshold_reduction_right": 0.085,
  "unwind_threshold_increase_left": 0.28,
  "unwind_threshold_increase_right": 0.30,
  "turn_in_friction_boost_left": 0.02,
  "turn_in_friction_boost_right": 0.01,
  "unwind_friction_reduction_left": 0.42,
  "unwind_friction_reduction_right": 0.44,
  "friction_scale_mult": 1.0,
  "center_taper_max": 0.17,
  "center_taper_lat": 0.16,
  "center_taper_lat_width": 0.04,
  "center_taper_speed": 15.0,
  "center_taper_speed_width": 2.2,
  "sustained_turn_in_ff_boost_left": 0.0,
  "sustained_turn_in_ff_boost_right": 0.0,
  "sustained_turn_in_ff_speed": 13.5,
  "sustained_turn_in_ff_speed_width": 1.8,
  "sustained_turn_in_ff_lat_start": 1.10,
  "sustained_turn_in_ff_lat_end": 3.60,
  "sustained_turn_in_ff_lat_width": 0.30,
  "steady_high_lat_taper": 0.015,
  "steady_high_lat_start": 0.35,
  "steady_high_lat_width": 0.12,
  "steady_jerk_width": 0.08,
  "hkg_friction_threshold": True,
  "damping_gain": 0.02,
  "turn_exit_damping_gain": 0.02,
  "turn_exit_damping_gain_right": 0.02,
  "reversal_damping_gain": 0.0175,
  "reversal_hold_seconds": 0.60,
  "steering_rate_feedback_gain": 0.0,
}
CURRENT_IONIQ5_CONTROLLER_PARAMS_SHA256 = "b88e9997a04b67b8acb979ec3e5b988a2c3caf081e988f14764e56e0149a9537"
REVIEWED_IONIQ5_CONTROLLER_PROFILES_BY_SOURCE_COMMIT = {
  "2747bf037c0f284500457f1befb4f52415e3285a": {
    "profile_id": HISTORICAL_IONIQ5_CONTROLLER_PROFILE_ID,
    "baseline_controller_params": (HISTORICAL_IONIQ5_CONTROLLER_PARAMS),
    "baseline_controller_params_sha256": (HISTORICAL_IONIQ5_CONTROLLER_PARAMS_SHA256),
  },
  REFINED_IONIQ5_CONTROLLER_SOURCE_COMMIT: {
    "profile_id": REFINED_IONIQ5_CONTROLLER_PROFILE_ID,
    "baseline_controller_params": (REFINED_IONIQ5_CONTROLLER_PARAMS),
    "baseline_controller_params_sha256": (REFINED_IONIQ5_CONTROLLER_PARAMS_SHA256),
  },
  CURRENT_IONIQ5_CONTROLLER_SOURCE_COMMIT: {
    "profile_id": CURRENT_IONIQ5_CONTROLLER_PROFILE_ID,
    "baseline_controller_params": (CURRENT_IONIQ5_CONTROLLER_PARAMS),
    "baseline_controller_params_sha256": (CURRENT_IONIQ5_CONTROLLER_PARAMS_SHA256),
  },
}
REVIEWED_IONIQ5_CONTROLLER_PROFILES_BY_ID = {
  profile["profile_id"]: {
    **profile,
    "source_starpilot_commit": source_commit,
  }
  for source_commit, profile in (REVIEWED_IONIQ5_CONTROLLER_PROFILES_BY_SOURCE_COMMIT.items())
}
CONTROLLER_PROFILE_EVALUATOR_NAME = "starpilot-ioniq5-controller-profile-by-source-commit"
CONTROLLER_PROFILE_EVALUATOR_VERSION = 1
CONTROLLER_PROFILE_EVALUATOR_SOURCE_SHA256 = hashlib.sha256(
  json.dumps(
    {
      "name": CONTROLLER_PROFILE_EVALUATOR_NAME,
      "version": CONTROLLER_PROFILE_EVALUATOR_VERSION,
      "car_fingerprint": "HYUNDAI_IONIQ_5",
      "profiles_by_source_commit": (REVIEWED_IONIQ5_CONTROLLER_PROFILES_BY_SOURCE_COMMIT),
    },
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
  ).encode(),
).hexdigest()
TORQUE_CONTEXT_2747BF_TUNING_LEVELS = {
  "AdvancedLateralTune": 2,
  "ForceAutoTune": 3,
  "ForceAutoTuneOff": 2,
  "LateralTune": 1,
  "NNFF": 2,
  "NNFFLite": 2,
  "SteerFriction": 3,
  "SteerLatAccel": 3,
}
TORQUE_CONTEXT_2747BF_BOOL_DEFAULTS = {
  "AdvancedLateralTune": True,
  "ForceAutoTune": False,
  "ForceAutoTuneOff": True,
  "LateralTune": True,
  "NNFF": False,
  "NNFFLite": False,
}
TORQUE_CONTEXT_EVALUATOR_NAME = "starpilot-torque-context-by-source-commit"
TORQUE_CONTEXT_EVALUATOR_VERSION = 1
TORQUE_CONTEXT_EVALUATOR_SOURCE_SHA256 = hashlib.sha256(
  json.dumps(
    {
      "bool_defaults": TORQUE_CONTEXT_2747BF_BOOL_DEFAULTS,
      "evaluators_by_source_commit": (KNOWN_TORQUE_CONTEXT_EVALUATORS),
      "live_use_params_policy": (
        "causal_liveTorqueParameters_useParams_else_initData_cache"
      ),
      "name": TORQUE_CONTEXT_EVALUATOR_NAME,
      "tuning_levels": TORQUE_CONTEXT_2747BF_TUNING_LEVELS,
      "version": TORQUE_CONTEXT_EVALUATOR_VERSION,
    },
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
  ).encode(),
).hexdigest()


def _baseline_controller_profile(
  route_software: dict[str, Any] | None,
  car_params: dict[str, Any] | None,
) -> dict[str, Any] | None:
  source_commit = route_software.get("git_source_commit") or route_software.get("git_commit") if route_software is not None else None
  car_fingerprint = car_params.get("car_fingerprint") if car_params is not None else None
  reviewed = REVIEWED_IONIQ5_CONTROLLER_PROFILES_BY_SOURCE_COMMIT.get(
    str(source_commit),
  )
  if reviewed is None or car_fingerprint != "HYUNDAI_IONIQ_5":
    return None
  baseline_params = reviewed["baseline_controller_params"]
  return {
    "profile_id": reviewed["profile_id"],
    "kernel_schema": ("comma-companion.ioniq5-torque-kernel"),
    "kernel_schema_version": 1,
    "source_starpilot_commit": source_commit,
    "baseline_controller_params": dict(baseline_params),
    "baseline_controller_params_sha256": (reviewed["baseline_controller_params_sha256"]),
    "effective_torque_params_value_space": ("raw_carparams_live_custom_pre_vehicle_multiplier"),
    "vehicle_lat_accel_factor_multiplier": (baseline_params["base_lat_accel_factor_mult"]),
    "evaluator": {
      "name": CONTROLLER_PROFILE_EVALUATOR_NAME,
      "version": CONTROLLER_PROFILE_EVALUATOR_VERSION,
      "source_commit": source_commit,
      "source_sha256": (CONTROLLER_PROFILE_EVALUATOR_SOURCE_SHA256),
    },
  }


def _historical_tuning_level(
  params: dict[str, Any],
) -> int | None:
  confirmed = _param_bool(params, "TuningLevelConfirmed")
  if confirmed is False:
    return 2
  if confirmed is not True:
    return None
  value = _param_float(params, "TuningLevel")
  if value is None or not float(value).is_integer():
    return None
  level = int(value)
  return level if 0 <= level <= 3 else None


def _historical_bool(
  params: dict[str, Any],
  key: str,
  tuning_level: int,
) -> bool | None:
  required_level = TORQUE_CONTEXT_2747BF_TUNING_LEVELS[key]
  if tuning_level < required_level:
    return False
  value = _param_bool(params, key)
  if value is not None:
    return value
  return TORQUE_CONTEXT_2747BF_BOOL_DEFAULTS.get(key)


def _fallback_resolved_toggles(
  route_software: dict[str, Any] | None,
  car_params: dict[str, Any] | None,
  *,
  live_use_params: bool | None = None,
  live_use_params_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
  commit = route_software.get("git_source_commit") or route_software.get("git_commit") if route_software is not None else None
  evaluator = KNOWN_TORQUE_CONTEXT_EVALUATORS.get(str(commit))
  params = route_software.get("controller_params", {}) if route_software is not None else {}
  tune = car_params.get("lateral_torque_tuning") if car_params is not None else None
  is_torque = car_params is not None and car_params.get("lateral_tuning_type") == "torque" and tune is not None
  is_angle = car_params is not None and str(car_params.get("steer_control_type", "")).endswith("angle")
  tuning_level = _historical_tuning_level(params) if evaluator is not None else None
  advanced = (
    _historical_bool(
      params,
      "AdvancedLateralTune",
      tuning_level,
    )
    if tuning_level is not None
    else None
  )
  base_factor = _finite(tune.get("lat_accel_factor")) if isinstance(tune, dict) else None
  base_friction = _finite(tune.get("friction")) if isinstance(tune, dict) else None
  custom_factor_raw = _param_float(params, "SteerLatAccel")
  custom_friction_raw = _param_float(params, "SteerFriction")
  custom_factor = base_factor
  custom_friction = base_friction
  if advanced and tuning_level is not None and tuning_level >= TORQUE_CONTEXT_2747BF_TUNING_LEVELS["SteerLatAccel"] and base_factor is not None:
    custom_factor = _clamp(
      custom_factor_raw if custom_factor_raw is not None else base_factor,
      base_factor * 0.5,
      base_factor * 1.5,
    )
  if advanced and tuning_level is not None and tuning_level >= TORQUE_CONTEXT_2747BF_TUNING_LEVELS["SteerFriction"] and base_friction is not None:
    custom_friction = _clamp(
      custom_friction_raw if custom_friction_raw is not None else base_friction,
      0.0,
      1.0,
    )

  cached_ltp_item = params.get("LiveTorqueParameters")
  cached_use_params = None
  if isinstance(cached_ltp_item, dict):
    if cached_ltp_item.get("size_bytes") == 0:
      cached_use_params = False
    encoded = cached_ltp_item.get("value_base64")
    if cached_use_params is None and isinstance(encoded, str):
      try:
        raw = base64.b64decode(encoded, validate=True)
        with capnp_log.LiveTorqueParametersData.from_bytes(raw) as cached:
          cached_use_params = bool(cached.useParams)
      except Exception:
        cached_use_params = None
  if evaluator is not None and cached_ltp_item is None:
    # Params.get returned no value in the historical implementation.
    cached_use_params = False
  cached_use_params_source = "initData.Params.LiveTorqueParameters.useParams"
  cached_use_params_identity = {
    "log_mono_time_ns": (route_software.get("log_mono_time_ns") if route_software is not None else None),
    "segment_num": (_integer(route_software.get("source_segment")) if route_software is not None else None),
    "source_ordinal": (_integer(route_software.get("source_ordinal")) if route_software is not None else None),
  }
  effective_use_params = cached_use_params
  effective_use_params_source = cached_use_params_source
  effective_use_params_identity = cached_use_params_identity
  if live_use_params is not None:
    effective_use_params = live_use_params
    effective_use_params_source = "liveTorqueParameters.useParams"
    effective_use_params_identity = live_use_params_identity

  force_auto_value = (
    _historical_bool(
      params,
      "ForceAutoTune",
      tuning_level,
    )
    if tuning_level is not None
    else None
  )
  force_auto_off_value = (
    _historical_bool(
      params,
      "ForceAutoTuneOff",
      tuning_level,
    )
    if tuning_level is not None
    else None
  )
  force_auto = bool(advanced and effective_use_params is False and is_torque and not is_angle and force_auto_value)
  force_auto_off = bool(advanced and effective_use_params is True and is_torque and not is_angle and force_auto_off_value)
  use_custom_factor = bool(
    (custom_factor is not None and base_factor is not None and round(custom_factor, 2) != round(base_factor, 2) and is_torque and not force_auto)
    or force_auto_off
  )
  use_custom_friction = bool(
    (custom_friction is not None and base_friction is not None and round(custom_friction, 2) != round(base_friction, 2) and is_torque and not force_auto)
    or force_auto_off
  )
  lateral_tune = (
    _historical_bool(
      params,
      "LateralTune",
      tuning_level,
    )
    if tuning_level is not None
    else None
  )
  requested_nnff = (
    _historical_bool(
      params,
      "NNFF",
      tuning_level,
    )
    if tuning_level is not None
    else None
  )
  requested_nnff_lite = (
    _historical_bool(
      params,
      "NNFFLite",
      tuning_level,
    )
    if tuning_level is not None
    else None
  )
  # The source tree alone does not prove which model assets were deployed on
  # the device. A requested full NNFF controller therefore requires a logged
  # resolved toggle snapshot instead of this initData fallback.
  nnff_asset_inventory_resolved = not bool(lateral_tune and requested_nnff)
  effective_nnff = False
  effective_nnff_lite = bool(lateral_tune and not effective_nnff and requested_nnff_lite)
  resolved_values = {
    "force_auto_tune": force_auto,
    "force_auto_tune_off": force_auto_off,
    "friction": custom_friction,
    "latAccelFactor": custom_factor,
    "lateral_tune": lateral_tune,
    "nnff": effective_nnff,
    "nnff_lite": effective_nnff_lite,
    "nnff_model_name": _param_text(params, "NNFFModelName"),
    "tuning_level": tuning_level,
    "use_custom_friction": use_custom_friction,
    "use_custom_latAccelFactor": use_custom_factor,
  }
  fallback_hash_payload = {
    "evaluator_id": evaluator,
    "source_commit": commit,
    "controller_params_sha256": (route_software.get("controller_params_sha256") if route_software is not None else None),
    "car_params_wire_sha256": (car_params.get("_wire_sha256") if car_params is not None else None),
    "use_params_source": effective_use_params_source,
    "use_params_source_identity": effective_use_params_identity,
    "values": resolved_values,
  }
  fallback_hash = hashlib.sha256(
    json.dumps(
      fallback_hash_payload,
      sort_keys=True,
      separators=(",", ":"),
      allow_nan=False,
    ).encode(),
  ).hexdigest()
  return {
    "resolved_toggles": resolved_values,
    "resolved_toggles_valid": (
      evaluator is not None
      and route_software is not None
      and car_params is not None
      and effective_use_params is not None
      and tuning_level is not None
      and advanced is not None
      and lateral_tune is not None
      and requested_nnff is not None
      and requested_nnff_lite is not None
      and nnff_asset_inventory_resolved
    ),
    "resolved_toggles_sha256": fallback_hash,
    "_event_valid": True,
    "_mono_ns": None,
    "_segment_num": (route_software.get("source_segment") if route_software is not None else None),
    "_source_ordinal": (route_software.get("source_ordinal") if route_software is not None else None),
    "context_source": "versioned_initData_fallback",
    "evaluator_id": evaluator,
    "cached_live_torque_use_params": cached_use_params,
    "cached_live_torque_use_params_source": (cached_use_params_source),
    "cached_live_torque_use_params_source_identity": (cached_use_params_identity),
    "effective_live_torque_use_params": effective_use_params,
    "effective_live_torque_use_params_source": effective_use_params_source,
    "effective_live_torque_use_params_source_identity": (
      effective_use_params_identity
    ),
    "nnff_asset_inventory_resolved": (nnff_asset_inventory_resolved),
  }


def _effective_torque_context(
  *,
  car_params: dict[str, Any] | None,
  route_software: dict[str, Any] | None,
  resolved_snapshot: dict[str, Any] | None,
  live_snapshot: dict[str, Any] | None,
  context_ns: int,
  previous_state: dict[str, Any] | None,
  initial_state_known: bool,
) -> dict[str, Any]:
  tune = car_params.get("lateral_torque_tuning") if car_params is not None else None
  base_factor = _finite(tune.get("lat_accel_factor")) if isinstance(tune, dict) else None
  base_offset = _finite(tune.get("lat_accel_offset")) if isinstance(tune, dict) else None
  base_friction = _finite(tune.get("friction")) if isinstance(tune, dict) else None

  context = resolved_snapshot
  context_source = "starpilotPlan.starpilotToggles"
  if context is None:
    live_use_params = (
      bool(live_snapshot.get("live_torque_in_use"))
      if live_snapshot is not None
      and live_snapshot.get("_event_valid") is True
      else None
    )
    live_use_params_identity = (
      {
        "log_mono_time_ns": str(live_snapshot["_mono_ns"]),
        "segment_num": _integer(live_snapshot.get("_segment_num")),
        "source_ordinal": _integer(live_snapshot.get("_source_ordinal")),
      }
      if live_use_params is not None
      else None
    )
    context = _fallback_resolved_toggles(
      route_software,
      car_params,
      live_use_params=live_use_params,
      live_use_params_identity=live_use_params_identity,
    )
    context_source = "versioned_initData_fallback"
  toggles = context.get("resolved_toggles", {})
  context_valid = bool(context.get("resolved_toggles_valid") and context.get("_event_valid", True))
  custom_factor = _finite(toggles.get("latAccelFactor"))
  custom_friction = _finite(toggles.get("friction"))
  force_auto = bool(toggles.get("force_auto_tune"))
  force_auto_off = bool(toggles.get("force_auto_tune_off"))
  use_custom_factor = bool(
    toggles.get("use_custom_latAccelFactor"),
  )
  use_custom_friction = bool(toggles.get("use_custom_friction"))

  live_age_us = (context_ns - int(live_snapshot["_mono_ns"])) // 1_000 if live_snapshot is not None else None
  previous_live_ns = live_snapshot.get("_previous_mono_ns") if live_snapshot is not None else None
  live_period_us = (int(live_snapshot["_mono_ns"]) - int(previous_live_ns)) // 1_000 if live_snapshot is not None and previous_live_ns is not None else None
  live_event_valid = bool(live_snapshot.get("_event_valid")) if live_snapshot is not None else None
  live_alive = bool(live_event_valid and live_age_us is not None and 0 <= live_age_us < LIVE_TORQUE_ALIVE_TIMEOUT_US)
  live_replay_age_ok = bool(live_age_us is not None and 0 <= live_age_us <= LIVE_TORQUE_MAX_AGE_US)
  live_frequency_ok = bool(live_snapshot and live_snapshot.get("_frequency_ok"))
  live_use_requested = bool(force_auto or (live_snapshot and live_snapshot.get("live_torque_in_use")))
  use_live = bool(live_use_requested and live_alive and live_replay_age_ok and live_frequency_ok)

  context_mono = _integer(context.get("_mono_ns"))
  if context_mono is None and route_software is not None:
    context_mono = _integer(
      route_software.get("log_mono_time_ns"),
    )
  live_mono = int(live_snapshot["_mono_ns"]) if live_snapshot is not None else None
  car_params_identity = {
    "log_mono_time_ns": (car_params.get("log_mono_time_ns") if car_params is not None else None),
    "segment_num": (_integer(car_params.get("source_segment")) if car_params is not None else None),
    "source_ordinal": (_integer(car_params.get("source_ordinal")) if car_params is not None else None),
  }
  live_identity = {
    "log_mono_time_ns": (str(live_mono) if live_mono is not None else None),
    "segment_num": (_integer(live_snapshot.get("_segment_num")) if live_snapshot is not None else None),
    "source_ordinal": (_integer(live_snapshot.get("_source_ordinal")) if live_snapshot is not None else None),
  }
  resolved_identity = {
    "log_mono_time_ns": (str(context_mono) if context_mono is not None else None),
    "segment_num": _integer(context.get("_segment_num")),
    "source_ordinal": _integer(context.get("_source_ordinal")),
  }

  if previous_state is None:
    factor = base_factor
    offset = base_offset
    friction = base_friction
    sources = {
      "factor": "car_params",
      "offset": "car_params",
      "friction": "car_params",
    }
    source_mono_ns = {
      "factor": None,
      "offset": None,
      "friction": None,
    }
    source_identity = {
      "factor": dict(car_params_identity),
      "offset": dict(car_params_identity),
      "friction": dict(car_params_identity),
    }
    missing = [] if initial_state_known else ["pre_extraction_torque_state"]
    last_update_ns = None
    state_origin = "car_params_controller_initialization_before_route_start" if initial_state_known else "unknown_pre_extraction_state"
  else:
    factor = previous_state.get(
      "effective_lat_accel_factor",
    )
    offset = previous_state.get(
      "effective_lat_accel_offset",
    )
    friction = previous_state.get("effective_friction")
    sources = dict(
      previous_state.get(
        "effective_torque_params_source",
        {},
      )
    )
    source_mono_ns = dict(
      previous_state.get(
        "_effective_torque_params_source_mono_ns",
        {},
      )
    )
    source_identity = {
      part: dict(identity)
      for part, identity in previous_state.get(
        "effective_torque_params_source_identity",
        {},
      ).items()
      if isinstance(identity, dict)
    }
    missing = list(
      previous_state.get(
        "effective_torque_params_missing_fields",
        (),
      )
    )
    last_update_ns = _integer(
      previous_state.get(
        "effective_torque_params_last_update_log_mono_time_ns",
      )
    )
    state_origin = str(
      previous_state.get(
        "effective_torque_state_origin",
        "unknown",
      )
    )

  update_requested = bool(use_live or use_custom_factor or use_custom_friction)
  state_held = not update_requested
  if update_requested:
    factor = base_factor
    offset = base_offset
    friction = base_friction
    sources = {
      "factor": "car_params",
      "offset": "car_params",
      "friction": "car_params",
    }
    source_mono_ns = {
      "factor": None,
      "offset": None,
      "friction": None,
    }
    source_identity = {
      "factor": dict(car_params_identity),
      "offset": dict(car_params_identity),
      "friction": dict(car_params_identity),
    }
    if use_live and live_snapshot is not None:
      if not use_custom_factor:
        factor = _finite(
          live_snapshot.get("live_lat_accel_factor"),
        )
        offset = _finite(
          live_snapshot.get("live_lat_accel_offset"),
        )
        sources["factor"] = "live_filtered"
        sources["offset"] = "live_filtered"
        source_mono_ns["factor"] = live_mono
        source_mono_ns["offset"] = live_mono
        source_identity["factor"] = dict(live_identity)
        source_identity["offset"] = dict(live_identity)
      if not use_custom_friction:
        friction = _finite(
          live_snapshot.get("live_friction"),
        )
        sources["friction"] = "live_filtered"
        source_mono_ns["friction"] = live_mono
        source_identity["friction"] = dict(live_identity)
    if use_custom_factor:
      factor = custom_factor
      sources["factor"] = "resolved_custom"
      source_mono_ns["factor"] = context_mono
      source_identity["factor"] = dict(resolved_identity)
    if use_custom_friction:
      friction = custom_friction
      sources["friction"] = "resolved_custom"
      source_mono_ns["friction"] = context_mono
      source_identity["friction"] = dict(resolved_identity)
    missing = []
    last_update_ns = context_ns
    state_origin = "controls_state_runtime_update"

  for name, value in (
    ("carParams.latAccelFactor", base_factor),
    ("carParams.latAccelOffset", base_offset),
    ("carParams.friction", base_friction),
    ("effectiveLatAccelFactor", factor),
    ("effectiveLatAccelOffset", offset),
    ("effectiveFriction", friction),
  ):
    if value is None:
      missing.append(name)
  if not context_valid:
    missing.append("resolvedRuntimeToggles")
  if context_mono is not None and context_mono > context_ns:
    missing.append("futureResolvedRuntimeToggles")
  if live_mono is not None and live_mono > context_ns:
    missing.append("futureLiveTorqueParameters")
  if live_use_requested and not use_live and not (use_custom_factor or use_custom_friction):
    missing.append("liveTorqueParameters.all_checks")
  for part in sources:
    identity = source_identity.get(part, {})
    if (
      not isinstance(identity, dict)
      or not isinstance(identity.get("log_mono_time_ns"), str)
      or _integer(identity.get("segment_num")) is None
      or _integer(identity.get("source_ordinal")) is None
    ):
      missing.append(
        f"effectiveTorqueSourceIdentity.{part}",
      )

  context_segment = _integer(context.get("_segment_num"))
  context_ordinal = _integer(
    context.get("_source_ordinal"),
  )

  return {
    "effective_lat_accel_factor": factor,
    "effective_lat_accel_offset": offset,
    "effective_friction": friction,
    "effective_torque_params_exact": not missing,
    "effective_torque_params_missing_fields": sorted(set(missing)),
    "effective_torque_params_source": sources,
    "effective_torque_params_source_identity": source_identity,
    "_effective_torque_params_source_mono_ns": (source_mono_ns),
    "effective_torque_params_stateful": True,
    "effective_torque_params_state_machine_version": 1,
    "effective_torque_params_value_space": ("raw_carparams_live_custom_pre_vehicle_multiplier"),
    "effective_torque_params_state_held": state_held,
    "effective_torque_state_origin": state_origin,
    "effective_torque_params_last_update_log_mono_time_ns": (str(last_update_ns) if last_update_ns is not None else None),
    "effective_torque_context_log_mono_time_ns": str(
      context_ns,
    ),
    "future_feedforward_params_source": (sources.get("offset")),
    "force_auto_tune": force_auto,
    "force_auto_tune_off": force_auto_off,
    "use_custom_lat_accel_factor": use_custom_factor,
    "use_custom_friction": use_custom_friction,
    "effective_resolved_toggles_sha256": context.get(
      "resolved_toggles_sha256",
    ),
    "effective_resolved_toggles_source": context_source,
    "effective_resolved_toggles_evaluator_id": (context.get("evaluator_id")),
    "effective_resolved_toggles_log_mono_time_ns": (str(context_mono) if context_mono is not None else None),
    "effective_resolved_toggles_segment_num": (context_segment),
    "effective_resolved_toggles_source_ordinal": (context_ordinal),
    "live_torque_event_valid": live_event_valid,
    "live_torque_alive": live_alive,
    "live_torque_replay_age_ok": live_replay_age_ok,
    "live_torque_frequency_ok": live_frequency_ok,
    "live_torque_period_us": live_period_us,
    "live_torque_cadence_policy": "causal_timestamp_history",
    "live_torque_cadence_policy_version": 1,
    "live_torque_used": use_live,
    "live_torque_source_log_mono_time_ns": (str(live_mono) if live_mono is not None else None),
    "live_torque_source_segment_num": (_integer(live_snapshot.get("_segment_num")) if live_snapshot is not None else None),
    "live_torque_source_ordinal": (_integer(live_snapshot.get("_source_ordinal")) if live_snapshot is not None else None),
  }


def _controller_selection(
  *,
  car_params: dict[str, Any] | None,
  route_software: dict[str, Any] | None,
  resolved_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
  context = resolved_snapshot
  source = "starpilotPlan.starpilotToggles"
  if context is None:
    context = _fallback_resolved_toggles(route_software, car_params)
    source = "versioned_initData_fallback"
  valid = bool(
    context.get("resolved_toggles_valid") and context.get("_event_valid", True) and car_params is not None and car_params.get("lateral_tuning_type") == "torque"
  )
  toggles = context.get("resolved_toggles", {})
  lateral_tune = toggles.get("lateral_tune")
  nnff = toggles.get("nnff")
  nnff_lite = toggles.get("nnff_lite")
  model_name = toggles.get("nnff_model_name")
  params = route_software.get("controller_params", {}) if route_software is not None else {}
  if not isinstance(lateral_tune, bool):
    lateral_tune = _param_bool(params, "LateralTune")
  controller_type = "unresolved"
  if valid:
    if nnff is True:
      controller_type = "nnff"
    elif nnff is False and nnff_lite is True:
      controller_type = "nnff_lite"
    elif nnff is False and nnff_lite is False:
      controller_type = "conventional_torque"
    else:
      valid = False
  context_mono = _integer(context.get("_mono_ns"))
  if context_mono is None and route_software is not None:
    context_mono = _integer(
      route_software.get("log_mono_time_ns"),
    )
  return {
    "controller_selection_source": source,
    "controller_selection_valid": valid,
    "controller_type": controller_type,
    "lateral_tune": (bool(lateral_tune) if isinstance(lateral_tune, bool) else None),
    "lateral_tune_available": isinstance(lateral_tune, bool),
    "nnff_capable": (
      bool(lateral_tune) and (bool(nnff) or bool(nnff_lite))
      if isinstance(lateral_tune, bool) and isinstance(nnff, bool) and isinstance(nnff_lite, bool)
      else None
    ),
    "nnff_capable_available": (isinstance(lateral_tune, bool) and isinstance(nnff, bool) and isinstance(nnff_lite, bool)),
    "nnff_model_name": (str(model_name) if isinstance(model_name, str) else None),
    "nnff_model_name_available": isinstance(model_name, str),
    "resolved_toggles_sha256": context.get(
      "resolved_toggles_sha256",
    ),
    "controller_resolved_toggles_log_mono_time_ns": (str(context_mono) if context_mono is not None else None),
    "controller_resolved_toggles_segment_num": (_integer(context.get("_segment_num"))),
    "controller_resolved_toggles_source_ordinal": (_integer(context.get("_source_ordinal"))),
    "controller_selection_evaluator_id": context.get(
      "evaluator_id",
    ),
  }


def _controller_runtime_context(
  route_software: dict[str, Any] | None,
  car_params: dict[str, Any] | None,
) -> dict[str, Any]:
  params = route_software.get("controller_params", {}) if route_software is not None else {}
  trial_applied = _param_bool(params, "FLMTrialApplied")
  profile_id = _param_text(params, "FLMActiveProfileId")
  overrides_item = params.get("FLMActiveOverrides")
  overrides_active = overrides_item.get("runtime_active") if isinstance(overrides_item, dict) else None
  flm_active = None
  if trial_applied is False:
    flm_active = False
  elif trial_applied is True and profile_id == "":
    flm_active = False
  elif trial_applied is True and profile_id is not None and profile_id != "" and overrides_active is not None:
    flm_active = bool(overrides_active)

  openpilot_longitudinal = car_params.get("openpilot_longitudinal_control") if car_params is not None else None
  advanced_longitudinal = _param_bool(
    params,
    "AdvancedLongitudinalTune",
  )
  trailer_load_text = _param_text(params, "TrailerLoad")
  trailer_load_lb = _finite(trailer_load_text)
  trailer_load_kg = None
  if openpilot_longitudinal is False:
    trailer_load_kg = 0.0
  elif openpilot_longitudinal is True and advanced_longitudinal is False:
    trailer_load_kg = 0.0
  elif openpilot_longitudinal is True and advanced_longitudinal is True and trailer_load_lb is not None:
    trailer_load_kg = min(max(trailer_load_lb, 0.0), 15_000.0) * POUND_TO_KILOGRAM

  return {
    "flm_active": flm_active,
    "flm_active_available": flm_active is not None,
    "flm_trial_applied": trial_applied,
    "flm_active_profile_id": profile_id,
    "flm_active_overrides_sha256": (overrides_item.get("sha256") if isinstance(overrides_item, dict) else None),
    "flm_active_overrides_runtime_active": overrides_active,
    "trailer_load_kg": trailer_load_kg,
    "trailer_load_available": trailer_load_kg is not None,
    "trailer_load_raw_lb": trailer_load_lb,
    "advanced_longitudinal_tune": advanced_longitudinal,
    "openpilot_longitudinal_control": openpilot_longitudinal,
  }


def _model_path_row(event: Any, event_t_us: int, segment_num: int, origin_ns: int) -> dict[str, Any]:
  model = event.modelV2
  position = _get(model, "position")
  timestamp_eof = _integer(_get(model, "timestampEof"))
  return {
    "t_us": _t_us(timestamp_eof, origin_ns) if timestamp_eof else event_t_us,
    "event_t_us": event_t_us,
    "segment_num": segment_num,
    "frame_id": _integer(_get(model, "frameId")),
    "frame_id_extra": _integer(_get(model, "frameIdExtra")),
    "timestamp_eof_ns": str(timestamp_eof or 0),
    "path": {
      "t_s": _float_list(_get(position, "t")),
      "x_m": _float_list(_get(position, "x")),
      "y_m": _float_list(_get(position, "y")),
      "z_m": _float_list(_get(position, "z")),
    },
    "action": {
      "desired_curvature_1pm": _finite(_get(model, "action", "desiredCurvature")),
      "desired_acceleration_mps2": _finite(_get(model, "action", "desiredAcceleration")),
      "should_stop": _boolean(_get(model, "action", "shouldStop")),
    },
  }


def _frame_row(event: Any, which: str, event_t_us: int, origin_ns: int) -> dict[str, Any]:
  index = getattr(event, which)
  timestamp_sof = _integer(_get(index, "timestampSof"))
  timestamp_eof = _integer(_get(index, "timestampEof"))
  if timestamp_eof:
    source_time_ns = timestamp_eof
    timestamp_source = "timestamp_eof"
    timestamp_quality = "exact_encoder_timestamp"
  elif timestamp_sof:
    source_time_ns = timestamp_sof
    timestamp_source = "timestamp_sof"
    timestamp_quality = "encoder_sof_fallback"
  else:
    source_time_ns = _integer(_get(event, "logMonoTime")) or origin_ns
    timestamp_source = "event_log_mono_time_fallback"
    timestamp_quality = "invalid_missing_encoder_timestamp"
  return {
    "t_us": _t_us(source_time_ns, origin_ns),
    "event_t_us": event_t_us,
    "log_mono_time_ns": str(_integer(_get(event, "logMonoTime")) or 0),
    "source_service": which,
    "frame_id": _integer(_get(index, "frameId")),
    "encode_id": _integer(_get(index, "encodeId")),
    "segment_num": _integer(_get(index, "segmentNum")),
    "segment_frame_id": _integer(_get(index, "segmentId")),
    # loggerd does not currently populate segmentIdEncode. Preserve an explicit
    # unsupported marker instead of exposing its repeated schema-default zero.
    "segment_encode_id": None,
    "segment_encode_id_supported": False,
    "timestamp_sof_ns": str(timestamp_sof or 0),
    "timestamp_eof_ns": str(timestamp_eof or 0),
    "timestamp_source": timestamp_source,
    "timestamp_quality": timestamp_quality,
    "event_valid": _event_valid(event),
    "flags": _integer(_get(index, "flags")),
    "bytes": _integer(_get(index, "len")),
    "encode_type": _text(_get(index, "type")),
  }


def _utc_anchor(event: Any, which: str, t_us: int) -> dict[str, Any] | None:
  if not _event_valid(event):
    return None
  data = getattr(event, which)
  utc_ms = _integer(_get(data, "unixTimestampMillis"))
  if utc_ms is None or utc_ms < 1_000_000_000_000:
    return None
  if which in ("gpsLocation", "gpsLocationExternal") and _boolean(_get(data, "hasFix")) is not True:
    return None
  if which == "liveLocationKalman":
    if _boolean(_get(data, "gpsOK")) is not True or _text(_get(data, "status")) != "valid":
      return None
  return {
    "t_us": t_us,
    "utc_us": str(utc_ms * 1_000),
    "source": which,
    "quality": "validated_gps_fix" if which != "liveLocationKalman" else "validated_localization_gps",
  }


def _onroad_event_rows(event: Any, which: str) -> list[dict[str, Any]]:
  data = getattr(event, which)
  values = data if which == "onroadEvents" else _get(data, "events")
  rows: list[dict[str, Any]] = []
  if values is None:
    return rows
  for value in values:
    rows.append(
      {
        "name": _text(_get(value, "name")) or "unknown",
        "enable": bool(_get(value, "enable")),
        "no_entry": bool(_get(value, "noEntry")),
        "warning": bool(_get(value, "warning")),
        "user_disable": bool(_get(value, "userDisable")),
        "soft_disable": bool(_get(value, "softDisable")),
        "immediate_disable": bool(_get(value, "immediateDisable")),
        "pre_enable": bool(_get(value, "preEnable")),
        "permanent": bool(_get(value, "permanent")),
        "override_lateral": bool(_get(value, "overrideLateral")),
        "override_longitudinal": bool(_get(value, "overrideLongitudinal")),
      }
    )
  return rows


def _event_severity(row: dict[str, Any]) -> str:
  if row["immediate_disable"] or row["soft_disable"]:
    return "critical"
  if row["warning"] or row["no_entry"] or row["permanent"]:
    return "warning"
  return "info"


def _sentinel_rows(
  events: Sequence[Any],
  segment_num: int,
  origin_ns: int,
) -> list[dict[str, Any]]:
  rows: list[dict[str, Any]] = []
  for ordinal, event in enumerate(events):
    if not _event_valid(event) or _event_which(event) != "sentinel":
      continue
    mono_ns = _integer(_get(event, "logMonoTime"))
    if mono_ns is None or mono_ns <= 0:
      continue
    sentinel = event.sentinel
    rows.append(
      {
        "type": _text(_get(sentinel, "type")),
        "signal": _integer(_get(sentinel, "signal")),
        "segment_num": segment_num,
        "source_ordinal": ordinal,
        "log_mono_time_ns": str(mono_ns),
        "t_us": _t_us(mono_ns, origin_ns),
      }
    )
  return rows


def _boundary_evidence(
  segment_num: int,
  sentinels: Sequence[dict[str, Any]],
) -> dict[str, Any]:
  starts = [row for row in sentinels if row["type"] in ("startOfRoute", "startOfSegment")]
  ends = [row for row in sentinels if row["type"] in ("endOfSegment", "endOfRoute")]
  start = starts[0] if starts else None
  end = ends[-1] if ends else None
  expected_start = "startOfRoute" if segment_num == 0 else "startOfSegment"
  return {
    "expected_start_type": expected_start,
    "start": start,
    "end": end,
    "start_valid": start is not None and start["type"] == expected_start,
    "end_valid": end is not None,
    "terminal_type": end["type"] if end is not None else None,
    "terminal_signal": end["signal"] if end is not None else None,
    "terminal_flush_observed": end is not None,
    "sentinels": list(sentinels),
  }


def _event_valid(event: Any) -> bool:
  return bool(_get(event, "valid"))


def _resolved_toggle_types_valid(decoded: dict[str, Any]) -> bool:
  for key, expected_type in REQUIRED_RESOLVED_TORQUE_TOGGLE_KEYS.items():
    if key not in decoded:
      return False
    value = decoded[key]
    if expected_type == (int, float):
      if isinstance(value, bool) or not isinstance(value, expected_type) or not math.isfinite(float(value)):
        return False
    elif not isinstance(value, expected_type):
      return False
  return True


def _dynamics_service_snapshot(
  event: Any,
  which: str,
  *,
  segment_num: int | None = None,
  source_ordinal: int | None = None,
) -> dict[str, Any] | None:
  mono_ns = _integer(_get(event, "logMonoTime"))
  if mono_ns is None:
    return None
  source = {
    "_mono_ns": mono_ns,
    "_segment_num": segment_num,
    "_source_ordinal": source_ordinal,
    "_event_valid": _event_valid(event),
  }
  if which == "carState":
    return {
      **source,
      **_car_state_dynamics(event),
    }
  if which == "carControl":
    data = event.carControl
    actuators = _get(data, "actuators")
    return {
      **source,
      "lat_active": bool(_get(data, "latActive")),
      "requested_torque": _finite(_get(actuators, "torque")),
      "desired_curvature": _finite(_get(actuators, "curvature")),
    }
  if which == "carOutput":
    return {
      **source,
      "applied_torque": _finite(
        _get(event.carOutput, "actuatorsOutput", "torque"),
      ),
    }
  if which == "controlsState":
    data = event.controlsState
    torque_state = None
    lateral = _get(data, "lateralControlState")
    try:
      if lateral is not None and lateral.which() == "torqueState":
        torque_state = lateral.torqueState
    except Exception:
      torque_state = None
    return {
      **source,
      "has_torque_state": torque_state is not None,
      "controls_desired_curvature": _finite(_get(data, "desiredCurvature")),
      "actual_lateral_accel": _finite(
        _get(torque_state, "actualLateralAccel"),
      ),
      "desired_lateral_accel": _finite(
        _get(torque_state, "desiredLateralAccel"),
      ),
      "desired_lateral_jerk": _finite(
        _get(torque_state, "desiredLateralJerk"),
      ),
      "controller_output": _finite(_get(torque_state, "output")),
      "controller_i": _finite(_get(torque_state, "i")),
      "saturated": bool(_get(torque_state, "saturated")),
    }
  if which == "liveTorqueParameters":
    data = event.liveTorqueParameters
    return {
      **source,
      "live_torque_valid": bool(_get(data, "liveValid")),
      "live_torque_in_use": bool(_get(data, "useParams")),
      "live_lat_accel_factor": _finite(
        _get(data, "latAccelFactorFiltered"),
      ),
      "live_lat_accel_offset": _finite(
        _get(data, "latAccelOffsetFiltered"),
      ),
      "live_friction": _finite(
        _get(data, "frictionCoefficientFiltered"),
      ),
    }
  if which == "liveParameters":
    return {
      **source,
      "roll": _finite(_get(event.liveParameters, "roll")),
    }
  if which == "starpilotPlan":
    text = _text(_get(event.starpilotPlan, "starpilotToggles")) or ""
    if not text:
      return None
    try:
      decoded = json.loads(text)
    except json.JSONDecodeError:
      return {
        **source,
        "resolved_toggles_valid": False,
        "resolved_toggles_sha256": hashlib.sha256(
          text.encode(),
        ).hexdigest(),
        "resolved_toggles": {},
      }
    if not isinstance(decoded, dict):
      decoded = {}
      valid = False
    else:
      valid = _resolved_toggle_types_valid(decoded)
    selected = {key: decoded[key] for key in RESOLVED_TOGGLE_ALLOWLIST if key in decoded and isinstance(decoded[key], (str, int, float, bool, type(None)))}
    return {
      **source,
      "resolved_toggles_valid": valid,
      "resolved_toggles_sha256": hashlib.sha256(
        text.encode(),
      ).hexdigest(),
      "resolved_toggles": dict(sorted(selected.items())),
    }
  return None


def _car_state_dynamics(event: Any) -> dict[str, Any]:
  data = event.carState
  return {
    "steering_angle_deg": _finite(_get(data, "steeringAngleDeg")),
    "steering_rate_deg": _finite(_get(data, "steeringRateDeg")),
    "steering_torque_eps": _finite(_get(data, "steeringTorqueEps")),
    "v_ego": _finite(_get(data, "vEgo")),
    "a_ego": _finite(_get(data, "aEgo")),
    "steering_pressed": bool(_get(data, "steeringPressed")),
  }


def _speed_fade(speed_mps: float) -> float:
  span = FF_ROLL_OFFSET_FADE_HIGH_MPS - FF_ROLL_OFFSET_FADE_LOW_MPS
  return min(
    1.0,
    max(0.0, (speed_mps - FF_ROLL_OFFSET_FADE_LOW_MPS) / span),
  )


def _dynamics_candidate(
  latest: dict[str, dict[str, Any]],
  car_params: dict[str, Any] | None,
  route_software: dict[str, Any] | None,
  grid_ns: int,
) -> tuple[dict[str, Any] | None, list[str]]:
  car_state = latest.get("carState")
  car_control = latest.get("carControl")
  controls_state = latest.get("controlsState")
  car_output = latest.get("carOutput")
  missing_services = [
    service
    for service, value in (
      ("carState", car_state),
      ("carControl", car_control),
      ("controlsState.torqueState", controls_state),
      ("carOutput", car_output),
    )
    if value is None
  ]
  if controls_state is not None and not controls_state["has_torque_state"]:
    missing_services.append("controlsState.torqueState")
  if missing_services:
    return None, sorted(set(missing_services))

  assert car_state is not None
  assert car_control is not None
  assert controls_state is not None
  assert car_output is not None
  applied = car_output.get("applied_torque")
  applied_source = "carOutput.actuatorsOutput.torque"

  desired_curvature = car_control.get("desired_curvature")
  if desired_curvature is None:
    desired_accel = controls_state.get("desired_lateral_accel")
    speed = car_state.get("v_ego")
    if desired_accel is not None and speed is not None:
      desired_curvature = desired_accel / max(speed**2, 0.3**2)

  tune = car_params.get("lateral_torque_tuning") if car_params is not None else None
  base_factor = tune.get("lat_accel_factor") if tune is not None else None
  base_offset = tune.get("lat_accel_offset") if tune is not None else None
  base_friction = tune.get("friction") if tune is not None else None
  deadzone = tune.get("steering_angle_deadzone_deg") if tune is not None else None
  effective = controls_state.get(
    "_effective_torque_context",
  )
  controller_selection = controls_state.get(
    "_controller_selection",
  )
  if not isinstance(effective, dict):
    return None, ["controlsState.effectiveTorqueContext"]
  if not isinstance(controller_selection, dict):
    return None, ["controlsState.controllerSelection"]
  baseline_profile = controls_state.get(
    "_baseline_controller_profile",
  )
  live = controls_state.get("_effective_live_snapshot")
  live_valid = bool(live and live.get("live_torque_valid"))
  live_in_use = bool(live and live.get("live_torque_in_use"))
  effective_offset = effective["effective_lat_accel_offset"]

  live_parameters = controls_state.get(
    "_effective_live_parameters_snapshot",
  )
  roll = live_parameters.get("roll") if live_parameters is not None else None
  live_parameters_age_us = (grid_ns - int(live_parameters["_mono_ns"])) // 1_000 if live_parameters is not None else None
  live_parameters_event_valid = bool(live_parameters.get("_event_valid")) if live_parameters is not None else None
  controls_curvature = controls_state.get("controls_desired_curvature")
  speed = car_state.get("v_ego")
  gravity_adjusted_missing = [
    name
    for name, value in (
      ("controlsState.desiredCurvature", controls_curvature),
      ("carState.vEgo", speed),
      ("liveParameters.roll", roll),
      (
        "liveParameters.Event.valid",
        True if live_parameters_event_valid else None,
      ),
      (
        "liveParameters.age",
        True if live_parameters_age_us is not None and 0 <= live_parameters_age_us <= LIVE_PARAMETERS_MAX_AGE_US else None,
      ),
    )
    if value is None
  ]
  gravity_adjusted_feedforward = None
  if not gravity_adjusted_missing:
    assert controls_curvature is not None
    assert speed is not None
    assert roll is not None
    fade = _speed_fade(speed)
    gravity_adjusted_feedforward = controls_curvature * speed**2 - roll * ACCELERATION_DUE_TO_GRAVITY * fade
  feedforward_missing = list(gravity_adjusted_missing)
  if effective_offset is None:
    feedforward_missing.append("effectiveLatAccelOffset")
  if not effective["effective_torque_params_exact"]:
    feedforward_missing.extend(
      effective["effective_torque_params_missing_fields"],
    )
  future_feedforward = None
  if not feedforward_missing:
    assert speed is not None
    assert effective_offset is not None
    assert gravity_adjusted_feedforward is not None
    fade = _speed_fade(speed)
    future_feedforward = gravity_adjusted_feedforward - effective_offset * fade

  requested = car_control.get("requested_torque")
  steer_limited = requested is not None and applied is not None and abs(requested - applied) > 1e-2
  min_steer_speed = car_params.get("min_steer_speed_mps") if car_params is not None else None
  known_integrator_freeze = bool(
    steer_limited or car_state["steering_pressed"] or (min_steer_speed is not None and speed is not None and speed < max(min_steer_speed, 0.3))
  )
  candidate = {
    "applied_torque": applied,
    "applied_torque_source": applied_source,
    "requested_torque": car_control.get("requested_torque"),
    "actual_lateral_accel": controls_state.get(
      "actual_lateral_accel",
    ),
    "steering_angle_deg": car_state["steering_angle_deg"],
    "steering_rate_deg": car_state["steering_rate_deg"],
    "steering_torque_eps": car_state["steering_torque_eps"],
    "v_ego": speed,
    "a_ego": car_state["a_ego"],
    "desired_curvature": desired_curvature,
    "controls_desired_curvature": controls_curvature,
    "desired_lateral_accel": controls_state.get(
      "desired_lateral_accel",
    ),
    "desired_lateral_jerk": controls_state.get(
      "desired_lateral_jerk",
    ),
    "controller_output": controls_state.get("controller_output"),
    "controller_i": controls_state.get("controller_i"),
    "lat_active": bool(car_control["lat_active"]),
    "driver_overlay": bool(
      car_control["lat_active"] and car_state["steering_pressed"],
    ),
    "saturated": bool(controls_state["saturated"]),
    "steer_limited_by_safety": steer_limited,
    "integrator_frozen": known_integrator_freeze,
    "integrator_freeze_exact": False,
    "live_torque_valid": live_valid,
    "live_torque_in_use": live_in_use,
    "live_torque_event_valid": effective["live_torque_event_valid"],
    "live_torque_alive": effective["live_torque_alive"],
    "live_torque_replay_age_ok": effective["live_torque_replay_age_ok"],
    "live_torque_frequency_ok": effective["live_torque_frequency_ok"],
    "live_torque_cadence_policy": effective["live_torque_cadence_policy"],
    "live_torque_cadence_policy_version": effective["live_torque_cadence_policy_version"],
    "live_torque_used": effective["live_torque_used"],
    "live_parameters_event_valid": live_parameters_event_valid,
    "live_lat_accel_factor": (live.get("live_lat_accel_factor") if live is not None else None),
    "live_lat_accel_offset": (live.get("live_lat_accel_offset") if live is not None else None),
    "live_friction": (live.get("live_friction") if live is not None else None),
    "base_lat_accel_factor": base_factor,
    "base_lat_accel_offset": base_offset,
    "base_friction": base_friction,
    "effective_lat_accel_factor": effective["effective_lat_accel_factor"],
    "effective_lat_accel_offset": effective_offset,
    "effective_friction": effective["effective_friction"],
    "effective_torque_params_exact": effective["effective_torque_params_exact"],
    "effective_torque_params_missing_fields": effective["effective_torque_params_missing_fields"],
    "effective_torque_params_source": effective["effective_torque_params_source"],
    "effective_torque_params_source_identity": effective["effective_torque_params_source_identity"],
    "effective_torque_params_stateful": effective["effective_torque_params_stateful"],
    "effective_torque_params_state_machine_version": (effective["effective_torque_params_state_machine_version"]),
    "effective_torque_params_value_space": effective["effective_torque_params_value_space"],
    "vehicle_lat_accel_factor_multiplier": (
      baseline_profile.get(
        "vehicle_lat_accel_factor_multiplier",
      )
      if isinstance(baseline_profile, dict)
      else None
    ),
    "baseline_controller_profile_id": (baseline_profile.get("profile_id") if isinstance(baseline_profile, dict) else None),
    "baseline_controller_params_sha256": (
      baseline_profile.get(
        "baseline_controller_params_sha256",
      )
      if isinstance(baseline_profile, dict)
      else None
    ),
    "baseline_controller_source_starpilot_commit": (baseline_profile.get("source_starpilot_commit") if isinstance(baseline_profile, dict) else None),
    "effective_torque_params_state_held": effective["effective_torque_params_state_held"],
    "effective_torque_state_origin": effective["effective_torque_state_origin"],
    "effective_torque_params_last_update_log_mono_time_ns": (effective["effective_torque_params_last_update_log_mono_time_ns"]),
    "effective_torque_context_log_mono_time_ns": (effective["effective_torque_context_log_mono_time_ns"]),
    "force_auto_tune": effective["force_auto_tune"],
    "force_auto_tune_off": effective["force_auto_tune_off"],
    "use_custom_lat_accel_factor": effective["use_custom_lat_accel_factor"],
    "use_custom_friction": effective["use_custom_friction"],
    "effective_resolved_toggles_sha256": effective["effective_resolved_toggles_sha256"],
    "effective_resolved_toggles_source": effective["effective_resolved_toggles_source"],
    "effective_resolved_toggles_evaluator_id": effective["effective_resolved_toggles_evaluator_id"],
    "effective_resolved_toggles_log_mono_time_ns": (effective["effective_resolved_toggles_log_mono_time_ns"]),
    "effective_resolved_toggles_segment_num": effective["effective_resolved_toggles_segment_num"],
    "effective_resolved_toggles_source_ordinal": effective["effective_resolved_toggles_source_ordinal"],
    "live_torque_source_log_mono_time_ns": effective["live_torque_source_log_mono_time_ns"],
    "live_torque_source_segment_num": effective["live_torque_source_segment_num"],
    "live_torque_source_ordinal": effective["live_torque_source_ordinal"],
    **controller_selection,
    "controller_params_sha256": controls_state.get(
      "_controller_params_sha256",
    ),
    "controller_params_log_mono_time_ns": controls_state.get(
      "_controller_params_log_mono_time_ns",
    ),
    "controller_params_segment_num": controls_state.get(
      "_controller_params_segment_num",
    ),
    "controller_params_source_ordinal": controls_state.get(
      "_controller_params_source_ordinal",
    ),
    "car_params_wire_sha256": (car_params.get("_wire_sha256") if car_params is not None else None),
    "car_params_scope": (car_params.get("_scope") if car_params is not None else None),
    "car_params_log_mono_time_ns": (car_params.get("log_mono_time_ns") if car_params is not None else None),
    "car_params_segment_num": (_integer(car_params.get("source_segment")) if car_params is not None else None),
    "car_params_source_ordinal": (_integer(car_params.get("source_ordinal")) if car_params is not None else None),
    "steering_angle_deadzone_deg": deadzone,
    "roll": roll,
    "gravity_adjusted_future_lateral_accel": (gravity_adjusted_feedforward),
    "future_feedforward_lateral_accel": future_feedforward,
    "future_feedforward_params_source": effective["future_feedforward_params_source"],
    "future_feedforward_eligible": not feedforward_missing,
    "future_feedforward_missing_fields": feedforward_missing,
    # initData does not prove all runtime tuning switches and internal
    # controller states. The value above is useful but never an exact replay
    # provenance claim.
    "future_feedforward_exact": False,
  }
  missing_fields = [
    field
    for field in DYNAMICS_REQUIRED_FINITE_FIELDS
    if field != "signed_steering_rate_deg_s" and (candidate.get(field) is None or not math.isfinite(float(candidate[field])))
  ]
  if missing_fields:
    return None, missing_fields
  return candidate, []


@dataclass(frozen=True)
class _EventRef:
  mono_ns: int
  segment_num: int
  source_ordinal: int
  continuity_group: int
  event: Any


class _DynamicsGridBuilder:
  def __init__(
    self,
    *,
    origin_ns: int,
    origin_stable: bool,
    process_car_params_snapshots: list[dict[str, Any]],
    process_route_software_snapshots: list[dict[str, Any]],
  ):
    self.origin_ns = origin_ns
    self.origin_stable = origin_stable
    self.process_car_params_snapshots = process_car_params_snapshots
    self.process_route_software_snapshots = process_route_software_snapshots
    self.latest: dict[str, dict[str, Any]] = {}
    self.latest_valid_fast: dict[str, dict[str, Any]] = {}
    self.next_grid_ns: int | None = None
    self.pending_rows: list[dict[str, Any]] = []
    self.current_group: int | None = None
    self.previous_emitted_grid_ns: int | None = None
    self.previous_emitted_steering_angle_deg: float | None = None
    self.drop_reasons: Counter[str] = Counter()
    self.quality_counts: Counter[str] = Counter()
    self.discarded_trailing_rows = 0
    self.source_age_maxima_us: Counter[str] = Counter()
    self.live_torque_intervals_us: deque[int] = deque(maxlen=40)
    self.effective_torque_state: dict[str, Any] | None = None
    self.controller_selection_state: dict[str, Any] | None = None
    self.group_initial_state_known = False

  def _reset(self, continuity_group: int) -> None:
    self.discarded_trailing_rows += len(self.pending_rows)
    self.pending_rows.clear()
    self.latest.clear()
    self.latest_valid_fast.clear()
    self.next_grid_ns = None
    self.current_group = continuity_group
    self.previous_emitted_grid_ns = None
    self.previous_emitted_steering_angle_deg = None
    self.live_torque_intervals_us.clear()
    self.effective_torque_state = None
    self.group_initial_state_known = bool(self.origin_stable and continuity_group == 0)

  def _snapshot(self, ref: _EventRef, which: str) -> None:
    snapshot = _dynamics_service_snapshot(
      ref.event,
      which,
      segment_num=ref.segment_num,
      source_ordinal=ref.source_ordinal,
    )
    if snapshot is None:
      return
    previous = self.latest.get(which)
    snapshot["_previous_mono_ns"] = previous["_mono_ns"] if previous is not None else None
    if which == "liveTorqueParameters" and previous is not None:
      interval_us = (ref.mono_ns - int(previous["_mono_ns"])) // 1_000
      if interval_us > 0:
        self.live_torque_intervals_us.append(interval_us)
      average_us = sum(self.live_torque_intervals_us) / len(self.live_torque_intervals_us) if self.live_torque_intervals_us else None
      recent = list(self.live_torque_intervals_us)[-4:]
      recent_average_us = sum(recent) / len(recent) if recent else None
      snapshot["_frequency_ok"] = any(
        average is not None and e_min <= 1_000_000.0 / average <= e_max
        for average, e_min, e_max in (
          (
            average_us,
            LIVE_TORQUE_MIN_FREQUENCY_HZ,
            LIVE_TORQUE_MAX_FREQUENCY_HZ,
          ),
          (
            recent_average_us,
            LIVE_TORQUE_MIN_FREQUENCY_HZ,
            LIVE_TORQUE_MAX_FREQUENCY_HZ,
          ),
        )
      )
    elif which == "liveTorqueParameters":
      snapshot["_frequency_ok"] = False
    self.latest[which] = snapshot
    if which in ("carState", "carControl", "carOutput", "controlsState") and snapshot["_event_valid"]:
      self.latest_valid_fast[which] = snapshot
    elif which != "carState" and which in self.latest_valid_fast:
      self.latest_valid_fast.pop(which)

  def _resolved_for_car_state(
    self,
    car_state: dict[str, Any],
  ) -> dict[str, Any] | None:
    return self.latest.get("starpilotPlan")

  def _process_initialization_snapshot(
    self,
    snapshots: Sequence[dict[str, Any]],
  ) -> dict[str, Any] | None:
    candidates = [
      snapshot
      for snapshot in snapshots
      if (
        snapshot.get("event_valid") is True
        and _integer(
          snapshot.get("log_mono_time_ns"),
        )
        is not None
      )
    ]
    if not candidates:
      return None
    # CarParams and initData serialize immutable process-initialization
    # inputs. Their logMonoTime is evidence-capture time, not the time when
    # the configuration took effect. Later copies are consistency evidence.
    return min(
      candidates,
      key=lambda snapshot: (
        int(snapshot["log_mono_time_ns"]),
        int(snapshot["source_segment"]),
        int(snapshot["source_ordinal"]),
      ),
    )

  def _bind_controls_state_context(self) -> None:
    controls_state = self.latest_valid_fast.get(
      "controlsState",
    )
    if controls_state is None:
      return
    controls_ns = int(controls_state["_mono_ns"])
    car_params = self._process_initialization_snapshot(
      self.process_car_params_snapshots,
    )
    route_software = self._process_initialization_snapshot(
      self.process_route_software_snapshots,
    )
    candidate_resolved_snapshot = self.latest.get(
      "starpilotPlan",
    )
    resolved_snapshot = (
      candidate_resolved_snapshot
      if (
        candidate_resolved_snapshot is not None
        and candidate_resolved_snapshot.get(
          "_event_valid",
        )
        is True
        and candidate_resolved_snapshot.get(
          "resolved_toggles_valid",
        )
        is True
      )
      else None
    )
    live_snapshot = self.latest.get(
      "liveTorqueParameters",
    )
    effective = _effective_torque_context(
      car_params=car_params,
      route_software=route_software,
      resolved_snapshot=resolved_snapshot,
      live_snapshot=live_snapshot,
      context_ns=controls_ns,
      previous_state=self.effective_torque_state,
      initial_state_known=(self.group_initial_state_known),
    )
    self.effective_torque_state = effective

    observed_selection = _controller_selection(
      car_params=car_params,
      route_software=route_software,
      resolved_snapshot=resolved_snapshot,
    )
    if self.controller_selection_state is None:
      initialization_known = bool(
        self.group_initial_state_known
        or (
          observed_selection.get(
            "controller_selection_source",
          )
          == "versioned_initData_fallback"
          and observed_selection.get(
            "controller_selection_evaluator_id",
          )
          in KNOWN_TORQUE_CONTEXT_EVALUATORS.values()
        )
      )
      self.controller_selection_state = {
        **observed_selection,
        "controller_selection_stateful": (initialization_known),
        "controller_selection_state_machine_version": 1,
        "controller_selection_binding": ("controlsd_initialization_once_per_route_process"),
        "controller_selection_context_log_mono_time_ns": (str(controls_ns)),
        "controller_selection_observed_consistent": True,
      }
    else:
      if observed_selection.get("controller_selection_valid") is True:
        observed_consistent = observed_selection.get("controller_type") == self.controller_selection_state.get(
          "controller_type",
        )
        self.controller_selection_state["controller_selection_observed_consistent"] = bool(
          self.controller_selection_state.get(
            "controller_selection_observed_consistent",
          )
          and observed_consistent
        )

    controls_state["_effective_torque_context"] = effective
    controls_state["_controller_selection"] = dict(
      self.controller_selection_state,
    )
    controls_state["_effective_live_snapshot"] = live_snapshot
    controls_state["_effective_live_parameters_snapshot"] = self.latest.get("liveParameters")
    controls_state["_effective_car_params_snapshot"] = car_params
    controls_state["_effective_route_software_snapshot"] = route_software
    controls_state["_controller_params_sha256"] = route_software.get("controller_params_sha256") if route_software is not None else None
    controls_state["_controller_params_log_mono_time_ns"] = route_software.get("log_mono_time_ns") if route_software is not None else None
    controls_state["_controller_params_segment_num"] = _integer(route_software.get("source_segment")) if route_software is not None else None
    controls_state["_controller_params_source_ordinal"] = _integer(route_software.get("source_ordinal")) if route_software is not None else None
    controls_state["_baseline_controller_profile"] = _baseline_controller_profile(
      route_software,
      car_params,
    )

  def _emit_tick(self, grid_ns: int) -> None:
    car_state = self.latest_valid_fast.get("carState")
    if car_state is None:
      self.drop_reasons["carState"] += 1
      return
    car_segment = int(car_state["_segment_num"])
    latest = {
      **self.latest,
      **self.latest_valid_fast,
    }

    required = {
      "carState": car_state,
      "carControl": self.latest_valid_fast.get("carControl"),
      "carOutput": self.latest_valid_fast.get("carOutput"),
      "controlsState": self.latest_valid_fast.get("controlsState"),
    }
    controls_state = required["controlsState"]
    car_params = (
      controls_state.get(
        "_effective_car_params_snapshot",
      )
      if controls_state is not None
      else None
    )
    route_software = (
      controls_state.get(
        "_effective_route_software_snapshot",
      )
      if controls_state is not None
      else None
    )
    fast_ages_ns: dict[str, int] = {}
    invalid_reasons: list[str] = []
    for service, snapshot in required.items():
      if snapshot is None:
        invalid_reasons.append(f"missing_required_service:{service}")
        continue
      age_ns = grid_ns - int(snapshot["_mono_ns"])
      fast_ages_ns[service] = age_ns
      if age_ns < 0:
        invalid_reasons.append(f"future_required_service:{service}")
      elif age_ns > DYNAMICS_REQUIRED_SOURCE_MAX_AGE_US * 1_000:
        invalid_reasons.append(f"stale_required_service:{service}")
      if not snapshot.get("_event_valid"):
        invalid_reasons.append(f"invalid_required_service:{service}")
    if invalid_reasons:
      for reason in sorted(set(invalid_reasons)):
        self.drop_reasons[reason] += 1
        self.quality_counts[reason] += 1
      return

    candidate, drop_fields = _dynamics_candidate(
      latest,
      car_params,
      route_software,
      grid_ns,
    )
    if candidate is None:
      for field in drop_fields:
        self.drop_reasons[field] += 1
      return
    if not candidate.get("effective_torque_params_exact"):
      self.drop_reasons["inexact_effective_torque_context"] += 1
      return
    if not candidate.get("controller_selection_valid"):
      self.drop_reasons["unresolved_controller_selection"] += 1
      return

    nominal_t_us = _t_us(grid_ns, self.origin_ns)
    source_ns = int(car_state["_mono_ns"])
    source_t_us = _t_us(source_ns, self.origin_ns)
    car_state_age_us = nominal_t_us - source_t_us
    ages_us = {service: nominal_t_us - _t_us(int(snapshot["_mono_ns"]), self.origin_ns) for service, snapshot in required.items() if snapshot is not None}
    continuous = self.previous_emitted_grid_ns is not None and grid_ns - self.previous_emitted_grid_ns == DYNAMICS_SAMPLE_PERIOD_US * 1_000
    quality_flags = [] if continuous else ["source_start"]
    steering_angle_deg = float(candidate["steering_angle_deg"])
    signed_steering_rate_deg_s = 0.0
    if continuous and self.previous_emitted_steering_angle_deg is not None:
      signed_steering_rate_deg_s = (steering_angle_deg - self.previous_emitted_steering_angle_deg) / (DYNAMICS_SAMPLE_PERIOD_US / 1_000_000.0)
    controls_state = required["controlsState"]
    assert controls_state is not None
    live = controls_state.get("_effective_live_snapshot")
    live_parameters = controls_state.get(
      "_effective_live_parameters_snapshot",
    )
    effective = controls_state.get(
      "_effective_torque_context",
      {},
    )
    effective_source_ages_us: dict[
      str,
      int | None,
    ] = {}
    for part, owner in candidate["effective_torque_params_source"].items():
      owner_mono = effective.get(
        "_effective_torque_params_source_mono_ns",
        {},
      ).get(part)
      effective_source_ages_us[part] = nominal_t_us - _t_us(int(owner_mono), self.origin_ns) if owner != "car_params" and owner_mono is not None else None
    candidate.update(
      {
        "t_us": nominal_t_us,
        "nominal_t_us": nominal_t_us,
        "nominal_log_mono_time_ns": str(grid_ns),
        "source_t_us": source_t_us,
        "source_time_error_us": -car_state_age_us,
        "log_mono_time_ns": str(source_ns),
        "segment_num": car_segment,
        "source_ordinal": int(car_state["_source_ordinal"]),
        "car_state_log_mono_time_ns": str(
          car_state["_mono_ns"],
        ),
        "car_state_segment_num": int(
          car_state["_segment_num"],
        ),
        "car_state_source_ordinal": int(
          car_state["_source_ordinal"],
        ),
        "car_control_log_mono_time_ns": str(
          required["carControl"]["_mono_ns"],
        ),
        "car_control_segment_num": int(
          required["carControl"]["_segment_num"],
        ),
        "car_control_source_ordinal": int(
          required["carControl"]["_source_ordinal"],
        ),
        "car_output_log_mono_time_ns": str(
          required["carOutput"]["_mono_ns"],
        ),
        "car_output_segment_num": int(
          required["carOutput"]["_segment_num"],
        ),
        "car_output_source_ordinal": int(
          required["carOutput"]["_source_ordinal"],
        ),
        "controls_state_log_mono_time_ns": str(
          controls_state["_mono_ns"],
        ),
        "controls_state_segment_num": int(
          controls_state["_segment_num"],
        ),
        "controls_state_source_ordinal": int(
          controls_state["_source_ordinal"],
        ),
        "continuous": continuous,
        "quality_flags": quality_flags,
        "car_state_age_us": car_state_age_us,
        "car_control_age_us": ages_us["carControl"],
        "car_output_age_us": ages_us["carOutput"],
        "controls_state_age_us": ages_us["controlsState"],
        "effective_torque_params_source_age_us": (effective_source_ages_us),
        "live_torque_age_us": (nominal_t_us - _t_us(int(live["_mono_ns"]), self.origin_ns) if live is not None else None),
        "live_parameters_age_us": (
          nominal_t_us
          - _t_us(
            int(live_parameters["_mono_ns"]),
            self.origin_ns,
          )
          if live_parameters is not None
          else None
        ),
        "signed_steering_rate_deg_s": signed_steering_rate_deg_s,
        "controller_i_timing": "post_update_asof_source_row",
      }
    )
    self.pending_rows.append(candidate)
    self.previous_emitted_grid_ns = grid_ns
    self.previous_emitted_steering_angle_deg = steering_angle_deg

  def _emit_before(self, mono_ns: int) -> None:
    while self.next_grid_ns is not None and self.next_grid_ns < mono_ns:
      self._emit_tick(self.next_grid_ns)
      self.next_grid_ns += DYNAMICS_SAMPLE_PERIOD_US * 1_000

  def add_event_group(
    self,
    refs: Sequence[_EventRef],
  ) -> list[dict[str, Any]]:
    if not refs:
      return []
    mono_ns = refs[0].mono_ns
    continuity_group = refs[0].continuity_group
    if self.current_group != continuity_group:
      self._reset(continuity_group)
    self._emit_before(mono_ns)
    saw_car_state = False
    saw_controls_state = False
    for ref in refs:
      which = _event_which(ref.event)
      if which not in (
        "carState",
        "carControl",
        "carOutput",
        "controlsState",
        "liveTorqueParameters",
        "liveParameters",
        "starpilotPlan",
      ):
        continue
      self._snapshot(ref, which)
      saw_car_state |= which == "carState" and _event_valid(ref.event)
      saw_controls_state |= which == "controlsState" and _event_valid(ref.event)
    if saw_controls_state:
      self._bind_controls_state_context()
    if saw_car_state and self.next_grid_ns is None:
      period_ns = DYNAMICS_SAMPLE_PERIOD_US * 1_000
      self.next_grid_ns = ((mono_ns + period_ns - 1) // period_ns) * period_ns
    if self.next_grid_ns == mono_ns:
      self._emit_tick(self.next_grid_ns)
      self.next_grid_ns += DYNAMICS_SAMPLE_PERIOD_US * 1_000
    if not saw_car_state:
      return []
    committed = self.pending_rows
    self.pending_rows = []
    return committed

  def finish(self) -> None:
    self.discarded_trailing_rows += len(self.pending_rows)
    self.pending_rows.clear()


def _alert_signature(event: Any) -> tuple[str, str, str, str] | None:
  state = event.selfdriveState
  alert_type = _text(_get(state, "alertType")) or ""
  text1 = _text(_get(state, "alertText1")) or ""
  text2 = _text(_get(state, "alertText2")) or ""
  status = _text(_get(state, "alertStatus")) or ""
  if not (alert_type or text1 or text2):
    return None
  return alert_type, text1, text2, status


def _warning(code: str, message: str, severity: str = "warning", **scope: Any) -> dict[str, Any]:
  return {
    "code": code,
    "severity": severity,
    "message": message,
    **scope,
  }


def _missing_segment_numbers(segments: Sequence[SegmentInput]) -> list[int]:
  numbers = [segment.number for segment in segments]
  if not numbers:
    return []
  existing = set(numbers)
  return [number for number in range(max(numbers) + 1) if number not in existing]


def _iter_route_records_legacy(route: RouteInput, chunk_size: int = 4096) -> Iterator[dict[str, Any]]:
  if not route.segments:
    raise ExtractionError("route has no segments")

  first_segment = route.segments[0]
  try:
    first_payload, first_digest, first_compression = _read_log(first_segment.path)
    first_events, first_corrupt = _read_events(first_payload)
  except Exception as exc:
    raise ExtractionError(f"cannot read first segment {first_segment.path}: {exc}") from exc
  origin_ns = _timeline_start_ns(first_events)
  if origin_ns is None:
    raise ExtractionError(f"first segment has no monotonic events: {first_segment.path}")

  signal_chunks = _SignalChunker(chunk_size)
  downsampler = _Downsampler()
  frame_chunks = _RecordChunker("frame_chunk", "camera", chunk_size)
  model_chunks = _RecordChunker("model_path_chunk", "source", max(16, min(256, chunk_size)))
  dynamics_chunks = _RecordChunker(
    "dynamics_chunk",
    "schema",
    max(16, min(1024, chunk_size)),
  )
  markers = _IntervalMarkers()
  warnings: list[dict[str, Any]] = []
  utc_anchors: list[dict[str, Any]] = []
  segment_reports: list[dict[str, Any]] = []
  source_objects: list[dict[str, Any]] = []
  service_counts: Counter[str] = Counter()
  frame_counts: Counter[str] = Counter()
  latest: dict[str, dict[str, Any]] = {}
  dynamics_history = _CausalServiceHistory()
  car_params: dict[str, Any] | None = None
  dynamics_car_params: dict[str, Any] | None = None
  car_params_wire_sha256: str | None = None
  car_params_wire_snapshots: list[dict[str, Any]] = []
  route_software: dict[str, Any] | None = None
  route_software_snapshots: list[dict[str, Any]] = []
  prior_car_time_ns: int | None = None
  prior_steering_angle_rad: float | None = None
  last_time_us = 0
  first_time_us = 0
  last_alert: tuple[str, str, str, str] | None = None
  last_device_onroad: bool | None = None
  last_controls_active: bool | None = None
  event_snapshots: dict[str, dict[str, dict[str, Any]]] = {
    "onroadEvents": {},
    "starpilotOnroadEvents": {},
  }
  marker_count = 0
  telemetry_gap_count = 0
  camera_gap_counts: Counter[str] = Counter()
  frame_quality_counts: Counter[str] = Counter()
  custom_schema_drops: Counter[tuple[int, str]] = Counter()
  previous_frames: dict[str, dict[str, int]] = {}
  previous_segment_num: int | None = None
  previous_terminal_type: str | None = None
  previous_segment_corrupt = False
  previous_segment_end_ns: int | None = None

  dynamics_previous_mono_ns: int | None = None
  dynamics_previous_angle_deg: float | None = None
  dynamics_nominal_t_us: int | None = None
  dynamics_break_pending = False
  dynamics_row_count = 0
  dynamics_nonzero_jerk_count = 0
  dynamics_feedforward_eligible_count = 0
  dynamics_drop_reasons: Counter[str] = Counter()
  dynamics_quality_counts: Counter[str] = Counter()
  dynamics_enabled = route.log_type == "rlog"

  missing_segments = _missing_segment_numbers(route.segments)
  if missing_segments:
    warnings.append(
      _warning(
        "missing_segments",
        "One or more segment numbers are absent from the supplied route.",
        missing_segment_numbers=missing_segments,
      )
    )

  first_sentinels = _sentinel_rows(
    first_events,
    first_segment.number,
    origin_ns,
  )
  first_boundary = _boundary_evidence(
    first_segment.number,
    first_sentinels,
  )
  origin_stable = first_segment.number == 0 and first_boundary["start_valid"]
  origin_id = hashlib.sha256(
    f"{route.route_id}\0{origin_ns}".encode(),
  ).hexdigest()
  yield {
    "record": "stream_header",
    "schema": CONTRACT_NAME,
    "schema_version": CONTRACT_VERSION,
    "extractor_version": EXTRACTOR_VERSION,
    "route_id": route.route_id,
    "log_type": route.log_type,
    "timebase": {
      "unit": "us",
      "origin_log_mono_time_ns": str(origin_ns),
      "origin_id": origin_id,
      "origin_stability": ("segment_zero_stable" if origin_stable else "provisional_supplied_subset"),
      "conversion": "floor((logMonoTime-origin)/1000)",
      "stable_sample_identity": [
        "route_id",
        "source_segment_num",
        "source_ordinal",
        "log_mono_time_ns",
      ],
    },
    "tiers": [{"id": "full", "width_us": None}] + [{"id": _tier_name(width), "width_us": width} for width in TIER_WIDTHS_US],
  }
  yield {
    "record": "signal_catalog",
    "signals": [spec.as_dict() for spec in SIGNAL_SPECS],
  }
  yield {
    "record": "dynamics_catalog",
    "schema": DYNAMICS_SCHEMA,
    "schema_version": DYNAMICS_SCHEMA_VERSION,
    "sample_period_us": DYNAMICS_SAMPLE_PERIOD_US,
    "required_source_max_age_us": (DYNAMICS_REQUIRED_SOURCE_MAX_AGE_US),
    "alignment": "timestamp_causal_recorded_history_asof",
    "resampling": "one row per eligible carState; zero-order hold from recorded observations no newer than the carState; no interpolation",
    "controller_i_timing": "post_update_asof_source_row",
    "signed_steering_rate": "causal derivative over retained eligible source rows; zero at first row and discontinuities",
    "availability": ("available" if dynamics_enabled else "unavailable_qlog_decimated"),
    "columns": list(DYNAMICS_COLUMNS),
  }
  if missing_segments:
    yield markers.point(
      0,
      kind="telemetry_gap",
      label="Missing route segments",
      severity="warning",
      attributes={"missing_segment_numbers": missing_segments},
    )
    marker_count += 1

  for segment_index, segment in enumerate(route.segments):
    if segment_index == 0:
      payload, digest, compression = first_payload, first_digest, first_compression
      events, corrupt = first_events, first_corrupt
    else:
      try:
        payload, digest, compression = _read_log(segment.path)
        events, corrupt = _read_events(payload)
      except Exception as exc:
        closing_markers = markers.close_all(last_time_us)
        marker_count += len(closing_markers)
        yield from closing_markers
        latest.clear()
        dynamics_history.clear()
        dynamics_car_params = None
        prior_car_time_ns = None
        prior_steering_angle_rad = None
        last_alert = None
        last_device_onroad = None
        last_controls_active = None
        event_snapshots = {
          "onroadEvents": {},
          "starpilotOnroadEvents": {},
        }
        previous_frames.clear()
        dynamics_break_pending = dynamics_row_count > 0
        dynamics_previous_mono_ns = None
        dynamics_previous_angle_deg = None
        warnings.append(
          _warning(
            "segment_read_failed",
            f"Could not read segment: {type(exc).__name__}.",
            severity="error",
            segment_num=segment.number,
          )
        )
        segment_reports.append(
          {
            "segment_num": segment.number,
            "directory_name": segment.directory_name,
            "state": "failed",
            "error_type": type(exc).__name__,
          }
        )
        previous_segment_num = segment.number
        previous_terminal_type = None
        previous_segment_corrupt = True
        previous_segment_end_ns = None
        continue

    source_objects.append(
      {
        "segment_num": segment.number,
        "sha256": digest,
        "log_type": route.log_type,
        "size_bytes": segment.path.stat().st_size,
        "compression": compression,
        "file_name": segment.path.name,
      }
    )
    if corrupt:
      warnings.append(
        _warning(
          "corrupt_events",
          "Cap'n Proto parsing stopped after a corrupt or truncated event.",
          segment_num=segment.number,
        )
      )

    sentinels = _sentinel_rows(events, segment.number, origin_ns)
    boundary = _boundary_evidence(segment.number, sentinels)
    event_mono_times = [
      mono for event in events if (mono := _integer(_get(event, "logMonoTime"))) is not None and mono > 0 and _event_which(event) != "initData"
    ]
    segment_first_mono_ns = min(event_mono_times) if event_mono_times else None
    segment_last_mono_ns = max(event_mono_times) if event_mono_times else None
    boundary_gap_ns = segment_first_mono_ns - previous_segment_end_ns if segment_first_mono_ns is not None and previous_segment_end_ns is not None else None
    carry_state = (
      segment_index > 0
      and previous_segment_num is not None
      and segment.number == previous_segment_num + 1
      and previous_terminal_type == "endOfSegment"
      and boundary["start"] is not None
      and boundary["start"]["type"] == "startOfSegment"
      and not previous_segment_corrupt
      and not corrupt
      and boundary_gap_ns is not None
      and -250_000_000 < boundary_gap_ns < 2_000_000_000
    )
    if segment_index > 0 and not carry_state:
      closing_markers = markers.close_all(last_time_us)
      marker_count += len(closing_markers)
      yield from closing_markers
      latest.clear()
      dynamics_history.clear()
      dynamics_car_params = None
      prior_car_time_ns = None
      prior_steering_angle_rad = None
      last_alert = None
      last_device_onroad = None
      last_controls_active = None
      event_snapshots = {
        "onroadEvents": {},
        "starpilotOnroadEvents": {},
      }
      previous_frames.clear()
      dynamics_break_pending = dynamics_row_count > 0
      dynamics_previous_mono_ns = None
      dynamics_previous_angle_deg = None
      warnings.append(
        _warning(
          "segment_state_discontinuity",
          "Causal service state was reset at a segment boundary that could not be proven contiguous.",
          severity="info",
          segment_num=segment.number,
          previous_segment_num=previous_segment_num,
          boundary_gap_ns=(str(boundary_gap_ns) if boundary_gap_ns is not None else None),
        )
      )

    segment_counts: Counter[str] = Counter()
    segment_start_us: int | None = None
    segment_end_us: int | None = None
    segment_camera_ranges: dict[str, list[int]] = {}
    segment_frame_issue_count = 0
    yield markers.point(
      max(0, _t_us(_timeline_start_ns(events) or origin_ns, origin_ns)),
      kind="segment_boundary",
      label=f"Segment {segment.number}",
      attributes={"segment_num": segment.number},
    )
    marker_count += 1

    # Observe messages in recorded file order, but only join service snapshots
    # whose own logMonoTime is at or before the triggering carState. This
    # prevents cross-service timestamp inversions from leaking future state.
    for source_ordinal, event in enumerate(events):
      which = _event_which(event)
      mono_ns = _integer(_get(event, "logMonoTime"))
      if which is None or mono_ns is None or mono_ns <= 0:
        continue
      if which == "initData":
        route_software = _init_data_snapshot(
          event,
          segment.number,
          max(0, _t_us(mono_ns, origin_ns)),
        )
        route_software_snapshots.append(route_software)
        continue
      if which == "carParams":
        car_params = _car_params_snapshot(
          event,
          segment.number,
          _t_us(mono_ns, origin_ns),
        )
        dynamics_car_params = car_params
        car_params_wire_sha256 = _struct_wire_sha256(event.carParams)
        car_params_wire_snapshots.append(
          {
            "segment_num": segment.number,
            "source_ordinal": source_ordinal,
            "log_mono_time_ns": str(mono_ns),
            "sha256": car_params_wire_sha256,
          }
        )
        continue
      if not dynamics_enabled:
        continue
      snapshot = _dynamics_service_snapshot(event, which)
      if which != "carState":
        if snapshot is not None:
          dynamics_history.add(which, snapshot)
        continue
      dynamics_latest = dynamics_history.snapshots_asof(mono_ns)
      candidate, drop_fields = _dynamics_candidate(
        event,
        dynamics_latest,
        dynamics_car_params,
      )
      if candidate is None:
        for field in drop_fields:
          dynamics_drop_reasons[field] += 1
        continue
      source_t_us = _t_us(mono_ns, origin_ns)
      car_control_age_us = (mono_ns - dynamics_latest["carControl"]["_mono_ns"]) // 1_000
      controls_state_age_us = (mono_ns - dynamics_latest["controlsState"]["_mono_ns"]) // 1_000
      car_output_age_us = (mono_ns - dynamics_latest["carOutput"]["_mono_ns"]) // 1_000 if "carOutput" in dynamics_latest else None
      required_ages = {
        "carControl": car_control_age_us,
        "controlsState": controls_state_age_us,
      }
      if candidate["applied_torque_source"] == "carOutput" and car_output_age_us is not None:
        required_ages["carOutput"] = car_output_age_us
      stale_services = sorted(service for service, age_us in required_ages.items() if age_us >= DYNAMICS_REQUIRED_SOURCE_MAX_AGE_US)
      if dynamics_previous_mono_ns is None:
        continuous = not dynamics_break_pending and not stale_services
        signed_rate = 0.0
      else:
        dt_s = (mono_ns - dynamics_previous_mono_ns) / 1e9
        continuous = 0.0001 < dt_s < 0.09 and not stale_services
        signed_rate = (
          (float(candidate["steering_angle_deg"]) - float(dynamics_previous_angle_deg)) / dt_s
          if continuous and dynamics_previous_angle_deg is not None
          else 0.0
        )
      if dynamics_nominal_t_us is None or dynamics_break_pending or (dynamics_previous_mono_ns is not None and not continuous):
        dynamics_nominal_t_us = (source_t_us + DYNAMICS_SAMPLE_PERIOD_US // 2) // DYNAMICS_SAMPLE_PERIOD_US * DYNAMICS_SAMPLE_PERIOD_US
      else:
        dynamics_nominal_t_us += DYNAMICS_SAMPLE_PERIOD_US
      candidate.update(
        {
          "t_us": dynamics_nominal_t_us,
          "nominal_t_us": dynamics_nominal_t_us,
          "source_t_us": source_t_us,
          "source_time_error_us": (source_t_us - dynamics_nominal_t_us),
          "log_mono_time_ns": str(mono_ns),
          "segment_num": segment.number,
          "source_ordinal": source_ordinal,
          "continuous": continuous,
          "quality_flags": (["stale_required_service:" + service for service in stale_services]),
          "car_control_age_us": car_control_age_us,
          "car_output_age_us": car_output_age_us,
          "controls_state_age_us": controls_state_age_us,
          "live_torque_age_us": (
            (mono_ns - dynamics_latest["liveTorqueParameters"]["_mono_ns"]) // 1_000 if "liveTorqueParameters" in dynamics_latest else None
          ),
          "live_parameters_age_us": ((mono_ns - dynamics_latest["liveParameters"]["_mono_ns"]) // 1_000 if "liveParameters" in dynamics_latest else None),
          "signed_steering_rate_deg_s": signed_rate,
          "controller_i_timing": "post_update_asof_source_row",
        }
      )
      if candidate["future_feedforward_eligible"]:
        dynamics_feedforward_eligible_count += 1
      for service in stale_services:
        dynamics_quality_counts["stale_required_service:" + service] += 1
      if abs(float(candidate["desired_lateral_jerk"])) > 1e-6:
        dynamics_nonzero_jerk_count += 1
      dynamics_row_count += 1
      dynamics_break_pending = False
      dynamics_previous_mono_ns = mono_ns
      dynamics_previous_angle_deg = float(
        candidate["steering_angle_deg"],
      )
      for record in dynamics_chunks.add(DYNAMICS_SCHEMA, candidate):
        yield record

    recorded_ordinals = {id(event): index for index, event in enumerate(events)}
    timeline_events = sorted(
      events,
      key=lambda event: (
        _integer(_get(event, "logMonoTime")) or 0,
        recorded_ordinals[id(event)],
      ),
    )
    event_before_origin_warned = False
    for event in timeline_events:
      source_ordinal = recorded_ordinals[id(event)]
      which = _event_which(event)
      mono_ns = _integer(_get(event, "logMonoTime"))
      if which is None or mono_ns is None or mono_ns <= 0:
        continue
      time_us = _t_us(mono_ns, origin_ns)
      segment_counts[which] += 1
      service_counts[which] += 1
      if which in ("initData", "carParams"):
        continue
      if time_us < 0 and not event_before_origin_warned:
        warnings.append(
          _warning(
            "event_before_origin",
            "One or more event timestamps predate the route-relative origin and were retained.",
            segment_num=segment.number,
            first_t_us=time_us,
          )
        )
        event_before_origin_warned = True
      last_time_us = max(last_time_us, time_us)
      first_time_us = min(first_time_us, time_us)
      segment_start_us = time_us if segment_start_us is None else min(segment_start_us, time_us)
      segment_end_us = time_us if segment_end_us is None else max(segment_end_us, time_us)

      fields = _event_fields(event, which)
      if fields:
        latest[which] = fields
        if which == "carState":
          current_angle = fields.get("vehicle.steering_angle")
          signed_rate = 0.0
          if isinstance(current_angle, float) and prior_steering_angle_rad is not None and prior_car_time_ns is not None:
            dt_s = (mono_ns - prior_car_time_ns) / 1e9
            if 0.0001 < dt_s < 0.09:
              signed_rate = (current_angle - prior_steering_angle_rad) / dt_s
            elif dt_s >= 0.25:
              yield markers.point(
                time_us,
                kind="telemetry_gap",
                label=f"{dt_s:.3f} s vehicle-state gap",
                severity="warning",
                attributes={
                  "gap_us": int(dt_s * 1e6),
                  "segment_num": segment.number,
                },
              )
              marker_count += 1
              telemetry_gap_count += 1
          fields["vehicle.steering_rate_signed"] = signed_rate
          prior_car_time_ns = mono_ns
          prior_steering_angle_rad = current_angle if isinstance(current_angle, float) else None

        for signal, value in fields.items():
          if signal not in SIGNAL_BY_ID or value is None:
            continue
          for record in signal_chunks.add(
            signal,
            "full",
            time_us,
            value,
            mono_ns,
            segment.number,
            source_ordinal,
          ):
            yield record
          for (
            tier_signal,
            tier,
            tier_time,
            tier_value,
            tier_mono,
            tier_segment,
            tier_ordinal,
          ) in downsampler.add(
            signal,
            SIGNAL_BY_ID[signal].value_type,
            time_us,
            value,
            mono_ns,
            segment.number,
            source_ordinal,
          ):
            for record in signal_chunks.add(
              tier_signal,
              tier,
              tier_time,
              tier_value,
              tier_mono,
              tier_segment,
              tier_ordinal,
            ):
              yield record

      if which in CAMERA_SERVICES:
        camera, file_name = CAMERA_SERVICES[which]
        row = _frame_row(event, which, time_us, origin_ns)
        row["source_file"] = file_name
        row["source_ordinal"] = source_ordinal
        frame_counts[camera] += 1
        frame_time_us = row["t_us"]
        camera_range = segment_camera_ranges.setdefault(
          camera,
          [frame_time_us, frame_time_us],
        )
        camera_range[0] = min(camera_range[0], frame_time_us)
        camera_range[1] = max(camera_range[1], frame_time_us)
        anomalies: list[str] = []
        if row["segment_num"] is not None and row["segment_num"] != segment.number:
          anomalies.append("encode_segment_num_mismatch")
        previous = previous_frames.get(camera)
        if previous is not None:
          if isinstance(row["frame_id"], int) and row["frame_id"] <= previous["frame_id"]:
            anomalies.append("frame_id_not_increasing")
          elif isinstance(row["frame_id"], int):
            missing_frames = row["frame_id"] - previous["frame_id"] - 1
            if missing_frames > 0:
              anomalies.append("frame_id_gap")
          if isinstance(row["encode_id"], int) and row["encode_id"] <= previous["encode_id"]:
            anomalies.append("encode_id_not_increasing")
          if frame_time_us <= previous["t_us"]:
            anomalies.append("timestamp_not_increasing")
          elif frame_time_us - previous["t_us"] > 200_000:
            anomalies.append("timestamp_gap")
          if previous["segment_num"] == segment.number and isinstance(row["segment_frame_id"], int):
            if row["segment_frame_id"] <= previous["segment_frame_id"]:
              anomalies.append("segment_frame_id_not_increasing")
            elif row["segment_frame_id"] > previous["segment_frame_id"] + 1:
              anomalies.append("segment_frame_id_gap")
          elif previous["segment_num"] != segment.number and isinstance(row["segment_frame_id"], int) and row["segment_frame_id"] > 0:
            anomalies.append("segment_frame_id_start_gap")
        elif isinstance(row["segment_frame_id"], int) and row["segment_frame_id"] > 0:
          anomalies.append("segment_frame_id_start_gap")
        if isinstance(row["frame_id"], int) and isinstance(row["encode_id"], int) and isinstance(row["segment_frame_id"], int):
          previous_frames[camera] = {
            "frame_id": row["frame_id"],
            "encode_id": row["encode_id"],
            "segment_frame_id": row["segment_frame_id"],
            "t_us": frame_time_us,
            "segment_num": segment.number,
          }
        if anomalies:
          unique_anomalies = sorted(set(anomalies))
          for anomaly in unique_anomalies:
            frame_quality_counts[f"{camera}:{anomaly}"] += 1
          segment_frame_issue_count += 1
          camera_gap_counts[camera] += 1
          yield markers.point(
            frame_time_us,
            kind="camera_gap",
            label=f"{camera} camera index anomaly",
            severity="warning",
            attributes={
              "camera": camera,
              "anomalies": unique_anomalies,
              "segment_num": segment.number,
            },
          )
          marker_count += 1
        for record in frame_chunks.add(camera, row):
          yield record

      if which == "modelV2":
        row = _model_path_row(
          event,
          time_us,
          segment.number,
          origin_ns,
        )
        row["log_mono_time_ns"] = str(mono_ns)
        row["source_ordinal"] = source_ordinal
        for record in model_chunks.add("modelV2.position", row):
          yield record

      if which in (
        "gpsLocation",
        "gpsLocationExternal",
        "liveLocationKalman",
      ):
        anchor = _utc_anchor(event, which, time_us)
        if anchor is not None:
          anchor["log_mono_time_ns"] = str(mono_ns)
          anchor["segment_num"] = segment.number
          anchor["source_ordinal"] = source_ordinal
          utc_anchors.append(anchor)

      if which == "deviceState":
        onroad = fields.get("device.onroad")
        if isinstance(onroad, bool):
          if last_device_onroad is None:
            yield markers.point(
              time_us,
              kind="onroad" if onroad else "offroad",
              label=("Device state observed onroad" if onroad else "Device state observed offroad"),
              attributes={
                "initial_state": True,
                "truncated_start": not origin_stable,
              },
            )
            marker_count += 1
          elif onroad != last_device_onroad:
            yield markers.point(
              time_us,
              kind="onroad" if onroad else "offroad",
              label=("Device went onroad" if onroad else "Device went offroad"),
            )
            marker_count += 1
          last_device_onroad = onroad
          closed = markers.transition(
            "onroad_interval",
            onroad,
            time_us,
            kind="onroad_interval",
            label="Device onroad",
          )
          marker_count += len(closed)
          yield from closed

      if which in ("carState", "carControl"):
        car = latest.get("carState", {})
        control = latest.get("carControl", {})
        overlay = bool(
          car.get("vehicle.steering_pressed"),
        ) and bool(control.get("control.lateral_active"))
        closed = markers.transition(
          "driver_overlay",
          overlay,
          time_us,
          kind="driver_overlay",
          label=("Driver steering input while lateral control was active"),
          severity="warning",
        )
        marker_count += len(closed)
        yield from closed

      if which == "controlsState":
        saturated = bool(fields.get("lateral.saturated"))
        closed = markers.transition(
          "lateral_saturation",
          saturated,
          time_us,
          kind="lateral_saturation",
          label="Lateral controller saturated",
          severity="warning",
        )
        marker_count += len(closed)
        yield from closed

      if which == "selfdriveState":
        active = bool(fields.get("selfdrive.active"))
        if last_controls_active is None:
          yield markers.point(
            time_us,
            kind="controls_state_observed",
            label=("Controls initially active" if active else "Controls initially inactive"),
            attributes={
              "active": active,
              "initial_state": True,
              "truncated_start": not origin_stable,
            },
          )
          marker_count += 1
        elif active != last_controls_active:
          yield markers.point(
            time_us,
            kind="engagement" if active else "disengagement",
            label=("Controls engaged" if active else "Controls disengaged"),
          )
          marker_count += 1
        last_controls_active = active
        closed = markers.transition(
          "controls_active",
          active,
          time_us,
          kind="controls_active",
          label="Controls active",
        )
        marker_count += len(closed)
        yield from closed
        alert = _alert_signature(event)
        if alert != last_alert:
          closed = markers.transition(
            "alert",
            False,
            time_us,
            kind="alert",
            label="",
          )
          marker_count += len(closed)
          yield from closed
          if alert is not None:
            alert_type, text1, text2, status = alert
            markers.transition(
              "alert",
              True,
              time_us,
              kind="alert",
              label=text1 or alert_type,
              severity=("critical" if status == "critical" else "warning"),
              attributes={
                "alert_type": alert_type,
                "text_2": text2,
                "status": status,
              },
            )
          last_alert = alert

      if which in ("onroadEvents", "starpilotOnroadEvents"):
        try:
          event_rows = _onroad_event_rows(event, which)
        except capnp.KjException:
          custom_schema_drops[(segment.number, which)] += 1
          continue
        current = {row["name"]: row for row in event_rows}
        previous = event_snapshots[which]
        for name in sorted(set(previous) | set(current)):
          if name not in current:
            old = previous[name]
            yield markers.point(
              time_us,
              kind="event_cleared",
              label=f"{name} cleared",
              severity="info",
              attributes={
                "source": which,
                "name": name,
                "previous": old,
              },
            )
            marker_count += 1
          elif name not in previous or current[name] != previous[name]:
            row = current[name]
            yield markers.point(
              time_us,
              kind="event",
              label=row["name"],
              severity=_event_severity(row),
              attributes={"source": which, **row},
            )
            marker_count += 1
        event_snapshots[which] = current

    preferred_camera = (
      "road"
      if "road" in segment_camera_ranges
      else (min(segment_camera_ranges, key=lambda camera: segment_camera_ranges[camera][0]) if segment_camera_ranges else None)
    )
    authoritative_start_us = segment_camera_ranges[preferred_camera][0] if preferred_camera is not None else None
    segment_state = "partial" if (corrupt or not boundary["start_valid"] or not boundary["end_valid"] or segment_frame_issue_count) else "complete"
    segment_reports.append(
      {
        "segment_num": segment.number,
        "directory_name": segment.directory_name,
        "state": segment_state,
        "range_us": [segment_start_us, segment_end_us],
        "start_t_us": authoritative_start_us,
        "start_time_source": (f"{preferred_camera}.encode_index.timestamp_eof" if preferred_camera is not None else None),
        "camera_ranges_us": dict(sorted(segment_camera_ranges.items())),
        "log_sha256": digest,
        "log_type": route.log_type,
        "source_size_bytes": segment.path.stat().st_size,
        "compression": compression,
        "event_count": sum(segment_counts.values()),
        "message_counts": dict(sorted(segment_counts.items())),
        "boundary": boundary,
        "frame_quality_issue_count": segment_frame_issue_count,
      }
    )
    previous_segment_num = segment.number
    previous_terminal_type = boundary["terminal_type"]
    previous_segment_corrupt = corrupt
    previous_segment_end_ns = segment_last_mono_ns

  for (
    signal,
    tier,
    tier_time,
    tier_value,
    tier_mono,
    tier_segment,
    tier_ordinal,
  ) in downsampler.flush():
    for record in signal_chunks.add(
      signal,
      tier,
      tier_time,
      tier_value,
      tier_mono,
      tier_segment,
      tier_ordinal,
    ):
      yield record
  yield from signal_chunks.flush()
  yield from frame_chunks.flush()
  yield from model_chunks.flush()
  yield from dynamics_chunks.flush()
  closing_markers = markers.close_all(last_time_us)
  marker_count += len(closing_markers)
  yield from closing_markers

  if car_params is None:
    warnings.append(
      _warning(
        "missing_car_params",
        "No carParams event was present in the supplied route segments; vehicle identity is unknown.",
      )
    )
  if custom_schema_drops:
    dropped = [
      {
        "segment_num": segment_num,
        "service": service,
        "count": count,
      }
      for (segment_num, service), count in sorted(custom_schema_drops.items())
    ]
    warnings.append(
      _warning(
        "custom_schema_mismatch",
        "Historical custom-schema messages could not be decoded with this checkout and were omitted.",
        severity="info",
        dropped_messages=dropped,
      )
    )
  if not utc_anchors:
    warnings.append(
      _warning(
        "missing_utc_anchor",
        "No trustworthy GPS or localization UTC anchor was present.",
        severity="info",
      )
    )
  if dynamics_drop_reasons:
    warnings.append(
      _warning(
        "dynamics_rows_dropped",
        "Some carState records lacked the causal service state or finite fields required by the dynamics contract.",
        severity="info",
        counts=dict(sorted(dynamics_drop_reasons.items())),
      )
    )
  if dynamics_quality_counts:
    warnings.append(
      _warning(
        "dynamics_alignment_limitations",
        "Some retained dynamics rows were marked discontinuous because a required causal service snapshot was at least 90 ms old.",
        counts=dict(sorted(dynamics_quality_counts.items())),
      )
    )
  if not dynamics_enabled:
    warnings.append(
      _warning(
        "dynamics_unavailable_qlog",
        (
          "qlog core control services are decimated to roughly 10 Hz; " + "visualization remains available, but 100 Hz dynamics rows " + "were not synthesized."
        ),
        severity="info",
      )
    )
  if dynamics_row_count and dynamics_nonzero_jerk_count == 0:
    warnings.append(
      _warning(
        "dynamics_jerk_derivation_required",
        (
          "All retained desiredLateralJerk values are zero. Historical "
          + "training derived jerk with a centered window; the causal "
          + "extractor did not substitute hindsight values."
        ),
        severity="info",
      )
    )
  if telemetry_gap_count:
    warnings.append(
      _warning(
        "telemetry_gaps",
        "One or more vehicle-state gaps exceeded 250 ms.",
        count=telemetry_gap_count,
      )
    )
  for camera, count in sorted(camera_gap_counts.items()):
    warnings.append(
      _warning(
        "camera_frame_gaps",
        f"One or more frame/time gaps were detected for {camera}.",
        camera=camera,
        count=count,
      )
    )
  for service in ("carState", "carControl", "carOutput", "controlsState"):
    if service_counts[service] == 0:
      warnings.append(
        _warning(
          "missing_service",
          f"No {service} events were present.",
          service=service,
        )
      )
  for camera in ("road", "wide", "driver", "qcamera"):
    if frame_counts[camera] == 0:
      warnings.append(
        _warning(
          "missing_camera_frame_map",
          f"No encoder time-map events were present for {camera}.",
          severity="info",
          camera=camera,
        )
      )

  car_params_summary_sha256 = None
  if car_params is not None:
    car_params_summary_sha256 = hashlib.sha256(
      json.dumps(
        car_params,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
      ).encode(),
    ).hexdigest()
  utc_start_us = None
  utc_solution = None
  if utc_anchors:
    offsets = [int(anchor["utc_us"]) - int(anchor["t_us"]) for anchor in utc_anchors]
    median_offset = int(round(statistics.median(offsets)))
    residuals = [abs(offset - median_offset) for offset in offsets]
    sorted_residuals = sorted(residuals)
    p95_index = min(
      len(sorted_residuals) - 1,
      int(0.95 * (len(sorted_residuals) - 1)),
    )
    utc_start_us = str(median_offset)
    utc_solution = {
      "method": "median_validated_gps_offset",
      "anchor_count": len(utc_anchors),
      "max_residual_us": max(residuals),
      "p95_residual_us": sorted_residuals[p95_index],
    }
    if max(residuals) > 1_000_000:
      warnings.append(
        _warning(
          "utc_anchor_inconsistent",
          "Validated UTC anchors disagree by more than one second.",
          max_residual_us=max(residuals),
        )
      )

  signals = []
  for spec in SIGNAL_SPECS:
    count = signal_chunks.sample_counts[spec.signal_id]
    if not count:
      continue
    signal = spec.as_dict()
    signal.update(
      {
        "sample_count": count,
        "coverage": [signal_chunks.ranges[spec.signal_id]],
      }
    )
    signals.append(signal)

  failed_segments = [report["segment_num"] for report in segment_reports if report["state"] == "failed"]
  parsed_reports = [report for report in segment_reports if report["state"] != "failed"]
  parsed_numbers = [report["segment_num"] for report in parsed_reports]
  route_start_observed = bool(
    parsed_reports
    and parsed_reports[0]["segment_num"] == 0
    and parsed_reports[0]["boundary"]["start_valid"]
    and parsed_reports[0]["boundary"]["start"]["type"] == "startOfRoute"
  )
  route_end_observed = bool(parsed_reports and parsed_reports[-1]["boundary"]["terminal_type"] == "endOfRoute")
  contiguous_from_zero = parsed_numbers == list(range(len(parsed_numbers))) and not failed_segments
  boundary_chain_valid = bool(parsed_reports)
  for report_index, report in enumerate(parsed_reports):
    boundary = report["boundary"]
    is_last = report_index == len(parsed_reports) - 1
    expected_terminal = "endOfRoute" if is_last else "endOfSegment"
    terminal_valid = boundary["terminal_type"] == expected_terminal
    if not terminal_valid:
      boundary_chain_valid = False
      if report["state"] == "complete":
        report["state"] = "partial"
      warnings.append(
        _warning(
          "segment_terminal_mismatch",
          "The segment terminal sentinel does not match its position in the supplied route.",
          segment_num=report["segment_num"],
          expected=expected_terminal,
          actual=boundary["terminal_type"],
        )
      )
  all_segments_complete = all(report["state"] == "complete" for report in parsed_reports)
  complete_route = (
    contiguous_from_zero
    and route_start_observed
    and route_end_observed
    and boundary_chain_valid
    and all_segments_complete
    and not missing_segments
    and not failed_segments
  )
  open_route = (
    contiguous_from_zero
    and route_start_observed
    and not route_end_observed
    and not missing_segments
    and not failed_segments
    and bool(parsed_reports)
    and parsed_reports[-1]["boundary"]["terminal_type"] is None
    and all(report["state"] == "complete" for report in parsed_reports[:-1])
  )
  route_state = "complete" if complete_route else ("open" if open_route else "partial")
  if not route_start_observed:
    warnings.append(
      _warning(
        "route_start_not_observed",
        "Segment zero with a startOfRoute sentinel is required for a stable published timeline.",
      )
    )
  if not route_end_observed:
    warnings.append(
      _warning(
        "route_end_not_observed",
        "An endOfRoute sentinel was not observed.",
        severity="info",
      )
    )

  timeline_version_hasher = hashlib.sha256()
  timeline_version_hasher.update(route.route_id.encode())
  timeline_version_hasher.update(b"\0")
  timeline_version_hasher.update(str(origin_ns).encode())
  for source in source_objects:
    timeline_version_hasher.update(b"\0")
    timeline_version_hasher.update(str(source["segment_num"]).encode())
    timeline_version_hasher.update(b":")
    timeline_version_hasher.update(source["sha256"].encode())
  timeline_version = timeline_version_hasher.hexdigest()
  extractor_source_sha256 = _extractor_source_sha256()
  extractor_dirty = _extractor_dirty()
  causal_input_eligible = dynamics_enabled and dynamics_row_count > 0
  unique_car_params_hashes = {snapshot["sha256"] for snapshot in car_params_wire_snapshots if snapshot["sha256"] is not None}
  if len(unique_car_params_hashes) > 1:
    warnings.append(
      _warning(
        "car_params_changed",
        "The wire-serialized carParams changed within the supplied route.",
        hashes=sorted(unique_car_params_hashes),
      )
    )
  unique_controller_params_hashes = {snapshot["controller_params_sha256"] for snapshot in route_software_snapshots}
  if len(unique_controller_params_hashes) > 1:
    warnings.append(
      _warning(
        "controller_params_changed",
        "The allowlisted controller-related initData params changed within the supplied route.",
        hashes=sorted(unique_controller_params_hashes),
      )
    )
  controller_params_sha256 = route_software.get("controller_params_sha256") if route_software is not None else None
  controller_runtime_context = _controller_runtime_context(
    route_software,
    car_params,
  )
  controller_runtime_snapshots = [
    {
      "segment_num": snapshot["source_segment"],
      "t_us": snapshot["t_us"],
      **_controller_runtime_context(snapshot, car_params),
    }
    for snapshot in route_software_snapshots
  ]
  baseline_limitations = [
    "runtime_controller_toggles_and_internal_state_are_not_fully_logged",
  ]
  if controller_params_sha256 is None:
    baseline_limitations.append(
      "allowlisted_initData_controller_params_unavailable",
    )
  if car_params_wire_sha256 is None:
    baseline_limitations.append("full_carParams_wire_snapshot_unavailable")
  if not controller_runtime_context["flm_active_available"]:
    baseline_limitations.append("flm_runtime_state_unavailable")
  if not controller_runtime_context["trailer_load_available"]:
    baseline_limitations.append("trailer_load_runtime_state_unavailable")

  manifest = {
    "record": "manifest",
    "schema": "comma-companion.telemetry-manifest",
    "schema_version": CONTRACT_VERSION,
    "route_id": route.route_id,
    "source_route": route.route_id,
    "state": route_state,
    "publication_ready": complete_route,
    "timeline_version": timeline_version,
    "timebase": {
      "unit": "us",
      "origin_log_mono_time_ns": str(origin_ns),
      "origin_id": origin_id,
      "origin_stability": ("segment_zero_stable" if route_start_observed else "provisional_supplied_subset"),
      "conversion": "floor((logMonoTime-origin)/1000)",
      "utc_start_us": utc_start_us,
      "utc_anchors": utc_anchors,
      "utc_solution": utc_solution,
      "stable_sample_identity": [
        "route_id",
        "source_segment_num",
        "source_ordinal",
        "log_mono_time_ns",
      ],
      "dependent_index_policy": "publish atomically under timeline_version; rebuild all relative-time media, telemetry, and bookmark indexes when it changes",
    },
    "range": {"start_us": first_time_us, "end_us": last_time_us},
    "tiers": [{"id": "full", "width_us": None}] + [{"id": _tier_name(width), "width_us": width} for width in TIER_WIDTHS_US],
    "signals": signals,
    "vehicle": car_params,
    "route_software": route_software,
    "frame_counts": dict(sorted(frame_counts.items())),
    "completeness": {
      "supplied_segment_count": len(route.segments),
      "supplied_segment_range": [
        min(segment.number for segment in route.segments),
        max(segment.number for segment in route.segments),
      ],
      "parsed_segment_numbers": parsed_numbers,
      "missing_segment_numbers": missing_segments,
      "failed_segment_numbers": failed_segments,
      "contiguous_from_segment_zero": contiguous_from_zero,
      "route_start_observed": route_start_observed,
      "route_end_observed": route_end_observed,
      "boundary_chain_valid": boundary_chain_valid,
      "segments": segment_reports,
    },
    "dynamics": {
      "schema": DYNAMICS_SCHEMA,
      "schema_version": DYNAMICS_SCHEMA_VERSION,
      "state": ("available" if dynamics_enabled else "unavailable_qlog_decimated"),
      "alignment": "timestamp_causal_recorded_history_asof",
      "sample_period_us": DYNAMICS_SAMPLE_PERIOD_US,
      "controller_i_timing": "post_update_asof_source_row",
      "row_count": dynamics_row_count,
      "nonzero_logged_jerk_row_count": dynamics_nonzero_jerk_count,
      "feedforward_eligible_row_count": dynamics_feedforward_eligible_count,
      "drop_counts": dict(sorted(dynamics_drop_reasons.items())),
      "quality_counts": dict(
        sorted(dynamics_quality_counts.items()),
      ),
      "causal_input_eligible": causal_input_eligible,
      "required_model_training_alignment": "timestamp_causal_recorded_history_asof",
      "telemetry_provenance": {
        "schema": DYNAMICS_SCHEMA,
        "schema_version": DYNAMICS_SCHEMA_VERSION,
        "alignment": "timestamp_causal_recorded_history_asof",
        "causal_input_eligible": causal_input_eligible,
        "extractor_source_sha256": extractor_source_sha256,
      },
      "controller_provenance": {
        "lateral_tuning_type": (car_params.get("lateral_tuning_type") if car_params is not None else None),
        "steer_control_type": (car_params.get("steer_control_type") if car_params is not None else None),
        "car_params_wire_sha256": car_params_wire_sha256,
        "controller_params_sha256": controller_params_sha256,
        **controller_runtime_context,
        "runtime_context_scope": ("segment initData snapshot; mid-segment Params changes are " + "not logged"),
        "runtime_snapshots": controller_runtime_snapshots,
        "baseline_exact_claim_allowed": False,
        "limitations": baseline_limitations,
      },
    },
    "capabilities": {
      "historical_custom_events": {
        "state": ("partial_schema_mismatch" if custom_schema_drops else "available"),
        "dropped_messages": [
          {
            "segment_num": segment_num,
            "service": service,
            "count": count,
          }
          for (segment_num, service), count in sorted(custom_schema_drops.items())
        ],
      },
      "segment_encode_id": {
        "state": "unsupported_not_populated_by_loggerd",
      },
      "exact_controller_baseline": {
        "state": "unavailable",
        "limitations": baseline_limitations,
      },
    },
    "provenance": {
      "extractor": "comma-companion-rlog",
      "extractor_version": EXTRACTOR_VERSION,
      "extractor_source_sha256": extractor_source_sha256,
      "extractor_dirty": extractor_dirty,
      "build_id": os.getenv("COMMA_COMPANION_BUILD_ID"),
      "starpilot_commit": _starpilot_commit(),
      "source_starpilot_commit": (route_software.get("git_source_commit") or route_software.get("git_commit") if route_software is not None else None),
      "cereal_schema_sha256": _schema_sha256(),
      "car_params_wire_sha256": car_params_wire_sha256,
      "car_params_wire_snapshots": car_params_wire_snapshots,
      "car_params_summary_sha256": car_params_summary_sha256,
      "controller_params_sha256": controller_params_sha256,
      "controller_params_snapshots": [
        {
          "segment_num": snapshot["source_segment"],
          "t_us": snapshot["t_us"],
          "sha256": snapshot["controller_params_sha256"],
        }
        for snapshot in route_software_snapshots
      ],
      "baseline_exact_claim_allowed": False,
      "baseline_limitations": baseline_limitations,
      "source_objects": source_objects,
    },
    "frame_quality": {
      "issues": dict(sorted(frame_quality_counts.items())),
    },
    "warnings": warnings,
  }
  yield manifest
  yield {
    "record": "stream_end",
    "status": route_state,
    "counts": {
      "signals": len(signals),
      "full_samples": sum(signal_chunks.sample_counts.values()),
      "frames": sum(frame_counts.values()),
      "dynamics_rows": dynamics_row_count,
      "markers": marker_count,
      "segments": len(segment_reports),
      "warnings": len(warnings),
    },
  }


# The bounded route-global implementation is kept separate so the evolving
# server contract does not entangle the low-level schema helpers above.
def iter_route_records(
  route: RouteInput,
  chunk_size: int = 4096,
) -> Iterator[dict[str, Any]]:
  from .stream_v2 import iter_route_records as implementation

  yield from implementation(route, chunk_size)
