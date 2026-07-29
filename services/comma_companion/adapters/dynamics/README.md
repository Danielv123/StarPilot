# Dynamics replay adapter

This adapter is the narrow boundary between the private archive server and the
StarPilot tuning work. It loads the reviewed Ioniq 5 neural plant artifact
directly, without importing the on-device controller or the training scripts.
The server communicates with it through newline-delimited JSON on stdin/stdout.

The only replay mode is `approximate_closed_loop`. It is deliberately read-only:
there is no method to write parameters to a comma.

## Runtime contract

Start the long-running process from the repository root:

```powershell
uv run --project services/comma_companion/adapters/dynamics `
  comma-companion-dynamics
```

Each input line is one request. Each output line contains the matching `id`,
an `ok` flag, and either `result` or `error`.

The replay request below is an abridged shape, not an eligible fixture. The
server request builder fills the complete historical controller profile and
the selected rows from reviewed telemetry.

```json
{"id":"info","method":"model_info","params":{}}
```

```json
{
  "id": "replay-1",
  "method": "replay",
  "params": {
    "mode": "approximate_closed_loop",
    "car_fingerprint": "HYUNDAI_IONIQ_5",
    "anchor_t_us": 123456789000,
    "horizon_s": 1.0,
    "source_log_type": "rlog",
    "input_alignment": "timestamp_causal_recorded_history_asof",
    "telemetry_provenance": {
      "schema": "comma-companion.dynamics-row",
      "schema_version": 1,
      "alignment": "timestamp_causal_recorded_history_asof",
      "causal_input_eligible": true,
      "extractor_version": "1.1.0",
      "extractor_source_sha256": "...",
      "route_origin_log_mono_time_ns": "123456780123"
    },
    "controller_i_timing": "post_update_asof_source_row",
    "controller_provenance": {
      "controller_type": "conventional_torque",
      "controller_type_verified": true,
      "car_params": {
        "car_fingerprint": "HYUNDAI_IONIQ_5",
        "lateral_tuning_type": "torque",
        "lateral_torque_tuning": {
          "lat_accel_factor": 3.172929,
          "lat_accel_offset": 0.0,
          "friction": 0.134187,
          "steering_angle_deadzone_deg": 0.0
        }
      },
      "car_params_sha256": "...",
      "car_params_complete": true,
      "car_params_provenance": {
        "wire_sha256": "...",
        "summary_sha256": "...",
        "source_starpilot_commit": "..."
      },
      "toggle_snapshot": {
        "controller_selection_source_types": ["starpilotPlan.starpilotToggles"],
        "resolved_toggles_sha256": ["..."],
        "flm_active": false,
        "flm_active_available": true,
        "flm_resolution": null,
        "trailer_load_kg": 0
      },
      "tuning_snapshot": {
        "lat_accel_factor": 3.172929,
        "lat_accel_offset": 0.0,
        "friction": 0.134187,
        "steering_angle_deadzone_deg": 0.0
      },
      "tuning_snapshot_complete": true,
      "tuning_provenance": {
        "controller_params_sha256": "...",
        "baseline_controller_profile": {
          "profile_id": "starpilot-ioniq5-torque-2747bf037c0f-v1",
          "kernel_schema": "comma-companion.ioniq5-torque-kernel",
          "kernel_schema_version": 1,
          "source_starpilot_commit": "2747bf037c0f284500457f1befb4f52415e3285a",
          "baseline_controller_params": {},
          "baseline_controller_params_sha256": "f8dc55e57772cd4850e37db43e494b4103384acc325627b0674a793952dd7a12",
          "effective_torque_params_value_space": "raw_carparams_live_custom_pre_vehicle_multiplier",
          "vehicle_lat_accel_factor_multiplier": 1.2101,
          "evaluator": {
            "name": "starpilot-ioniq5-controller-profile-by-source-commit",
            "version": 1,
            "source_commit": "2747bf037c0f284500457f1befb4f52415e3285a",
            "source_sha256": "..."
          }
        },
        "controller_selection_validation": {
          "schema": "comma-companion.controller-selection-proof",
          "schema_version": 1,
          "state": "verified",
          "checked_row_count": 12345,
          "missing_row_count": 0,
          "invalid_row_count": 0,
          "evaluator": {
            "name": "starpilot-controlsd-lateral-selection",
            "version": 1,
            "source_sha256": "..."
          }
        },
        "effective_torque_context_validation": {
          "schema": "comma-companion.effective-torque-context-proof",
          "schema_version": 1,
          "state": "verified",
          "checked_row_count": 12345,
          "exact_row_count": 12345,
          "valid_row_count": 12345,
          "inexact_row_count": 0,
          "missing_field_row_count": 0,
          "invalid_row_count": 0,
          "stateful_invalid_row_count": 0,
          "context_not_bound_to_controls_row_count": 0,
          "source_after_controls_row_count": 0,
          "source_identity_invalid_count": 0,
          "factor_source_counts": {"car_params": 12345},
          "offset_source_counts": {"car_params": 12345},
          "friction_source_counts": {"car_params": 12345},
          "evaluator": {
            "name": "starpilot-torque-context-by-source-commit",
            "version": 1,
            "source_sha256": "..."
          }
        }
      }
    },
    "baseline_params": {},
    "candidate_params": {"damping_gain": 0.0175},
    "rows": []
  }
}
```

`rows` must include 300 history samples before the anchor, the anchor itself,
and one additional row per requested prediction step. `nominal_t_us` is the
deterministic route-relative clock and must advance without missing
10,000-microsecond ticks. `nominal_log_mono_time_ns` is a decimal string on
the absolute monotonic-time grid; it must be divisible by 10,000,000 and
advance by exactly that amount. The manifest pins
`route_origin_log_mono_time_ns`, and each row must satisfy
`nominal_t_us=(nominal_log_mono_time_ns-route_origin_log_mono_time_ns)//1000`.
The route-relative clock may therefore have a constant nonzero 10 ms phase.
`source_t_us` identifies the latest causal carState used at each grid tick.
Repeated source timestamps are valid zero-order-held filler ticks, so a raw
20-millisecond carState skip is represented as two 10-millisecond plant steps
rather than one stretched step. `source_time_error_us` must equal
`-car_state_age_us` and cannot be positive.
`source_log_type` must be `rlog`; qlog dynamics services are about 10 Hz and
cannot be repeated or interpolated into an eligible 100 Hz tensor.
Every joined source, including carState, must carry a nonnegative age. A
negative age proves a future-leaking join, and missing age/source provenance
cannot be called causal. The promoted plant metadata sets
`max_asof_age_ms=35`; an applicable source older than 35 milliseconds or a row
marked discontinuous blocks eligibility.
The default horizon is 1 second and the hard maximum is 2 seconds.

Required row fields:

```text
nominal_t_us
nominal_log_mono_time_ns
source_t_us
source_time_error_us
continuous
car_control_age_us
car_state_age_us
car_output_age_us
controls_state_age_us
live_torque_age_us
live_parameters_age_us
live_parameters_event_valid
live_torque_event_valid
live_torque_alive
live_torque_frequency_ok
live_torque_used
live_torque_cadence_policy
live_torque_cadence_policy_version
effective_torque_params_exact
effective_torque_params_missing_fields
effective_torque_params_source
applied_torque_source
controller_type
controller_selection_source
controller_selection_stateful
controller_selection_state_machine_version
resolved_toggles_sha256
effective_torque_params_source_age_us
effective_torque_params_stateful
effective_torque_params_state_machine_version
effective_torque_params_value_space
vehicle_lat_accel_factor_multiplier
baseline_controller_profile_id
baseline_controller_params_sha256
baseline_controller_source_starpilot_commit
applied_torque
actual_lateral_accel
steering_angle_deg
steering_rate_deg
signed_steering_rate_deg_s
steering_torque_eps
v_ego
a_ego
desired_lateral_accel
gravity_adjusted_future_lateral_accel
future_feedforward_lateral_accel
future_feedforward_exact
desired_lateral_jerk
controller_output
controller_i
lat_active
driver_overlay
saturated
steer_limited_by_safety
integrator_frozen
integrator_freeze_exact
```

Rows also carry the effective live/base lateral-acceleration factor, offset,
friction, steering-angle deadzone, and any available safety-limit state.
`applied_torque_source` must be exactly
`carOutput.actuatorsOutput.torque`; a requested `carControl` torque is never
accepted as applied plant input. The recorded requested command, measured
applied torque, and reused actuator-limiter gap remain separate traces.
The plant's 35-millisecond age limit applies only to carState, carControl,
controlsState, and measured carOutput. Controller diagnostics use separate
causal gates: liveParameters requires a valid Event and an age no greater than
250 milliseconds. `live_torque_used` records the controller's effective
selection, including force-auto behavior, independently of payload
`liveValid`/`useParams`. When selected, liveTorqueParameters requires Event
validity, causally reconstructed alive/frequency proof, complete live
factor/offset/friction values, and an age no greater than one second. The
cadence proof is versioned as
`causal_timestamp_history` version 1 and is not a wire field.
`effective_torque_params_exact=true` with no missing fields separately proves
which live/base/custom factor, offset, and friction values the controller
actually used. Its factor/offset/friction ownership values are restricted to
`car_params`, `live_filtered`, or `resolved_custom`; fallback guesses or a
source/value mismatch are blocking.
The request also carries the route-wide effective-torque proof verbatim. Its
checked, exact, and valid row counts; every zero-failure counter; and each
factor/offset/friction source-count total must agree. The adapter pins the
versioned torque-context evaluator digest rather than accepting an arbitrary
well-formed hash. A route using the versioned initData fallback must additionally
bind the exact commit-specific evaluator ID, source commit, and digest.
Controller selection is initialized once and then held statefully, matching
`Controls.__init__`; a later toggle observation cannot rewrite the historical
controller type. Effective torque parameters use a separate state machine so
the last live/custom values remain in force when runtime stops updating them.
Each non-CarParams owner carries its actual source age, which must be at least
the joined controlsState age so values observed after the logged controller
output cannot leak backward into replay.
The value space is explicitly
`raw_carparams_live_custom_pre_vehicle_multiplier`. The reviewed historical
profile supplies the vehicle multiplier separately, preventing both omission
and double application.
Missing context keeps a trace visible but makes it ineligible. The raw unsigned
`steering_rate_deg` is required for eligibility; deriving its magnitude from
the signed rate is only a display fallback. The setpoint
`desired_lateral_accel`, roll-compensated
`gravity_adjusted_future_lateral_accel`, and final offset-adjusted
`future_feedforward_lateral_accel` are deliberately separate. The controller
kernel consumes the final field and never subtracts the lateral-acceleration
offset a second time.
If safety limiting and internal unwind state cannot prove every integrator
freeze decision, `integrator_freeze_exact=false` keeps the approximate trace
available and adds a prominent confidence warning. Likewise,
`future_feedforward_exact=false` warns that unlogged controller overrides may
change the reconstructed feedforward value. Neither flag alone blocks
`approximate_closed_loop`, but the response always says
`exact_baseline=false`.

The response contains recorded, baseline, and candidate traces; ensemble
uncertainty; fit-to-recorded metrics; parameter provenance; and explicit
quality warnings. Counterfactual command traces distinguish
`requested_output`, `reused_actuator_limiter_gap`, and
`modeled_applied_torque`. Low speed, inactive control, driver overlay,
saturation, timing gaps, weak baseline fit, model disagreement, incomplete
controller provenance, and out-of-distribution inputs make `eligible=false`.
Optimizer-only parameters are labeled `scope=model_only` in the parameter
schema.

## Provenance and deployment

The default artifact is:

```text
artifacts/tuning/neural_lateral_plant_20260723/neural_lateral_plant.pt
sha256 fb1b8b951fdff19ff5f9349470415d003b61ee5655fff996365b429d93f6dc45
```

The adapter rejects a different hash by default. Set `COMMA_DYNAMICS_MODEL` to
move the same artifact. Artifact selection is an explicit registry in
`plant.py`; an unregistered local artifact is always marked unreviewed and
causal-ineligible even when hash enforcement is disabled.

The currently promoted artifact was trained with legacy rlog file-order
latest-state joins. Those joins can include service messages timestamped after
the triggering carState, so its provenance is
`training_alignment=legacy_file_order_noncausal` and every causal replay is
blocked. It remains loadable only so historical traces and diagnostics stay
visible. A causally retrained, reviewed artifact must be registered and
explicitly promoted before the adapter can return `eligible=true` with real
telemetry.

Eligibility requires two independent claims: the telemetry manifest must say
`causal_input_eligible=true`, and the registered artifact must say
`causal_training_eligible=true`. The artifact's training alignment and dynamics
schema/version must match, and its reviewed
`compatible_telemetry_extractor_version` and
`compatible_telemetry_extractor_sha256` must equal the telemetry extractor
version and source hash. The actual training-extractor hash remains separate provenance;
similar-looking local artifacts are not accepted.

Controller profiles are also explicit and reviewed. The early `6dd6c0...`
Ioniq 5 profile uses its original `1.2507` multiplier and has canonical
parameter hash
`f02fff8adef34f524607bc0f200ad486523850a2ec576c36d664d9b369175ce8`.
The later `2747bf...` historical profile contains every
`ControllerParameters` field, including its `1.2101` multiplier, legacy
speed-scaled friction threshold, `0.729` friction scale, and neutral values for
later damping/steady-state additions. The separate `19f8c...` profile contains
the current `1.36` tune and has canonical parameter hash
`b88e9997a04b67b8acb979ec3e5b988a2c3caf081e988f14764e56e0149a9537`.
The request's complete `baseline_params` map, embedded profile map, canonical
JSON SHA-256, source commit, versioned evaluator, and every selected row must
all agree. Candidate edits are applied over the selected route's reviewed
baseline; current defaults never silently replace historical values, and
unknown future commits remain ineligible until their controller profile is
reviewed.
The reviewed model metadata also binds the sampling contract:
`grid=absolute_monotonic_time`, 10,000,000 ns/100 Hz,
`alignment=latest_at_or_before_grid_time_zero_order_hold`,
`max_asof_age_ms=35`, and deterministic event order
`[logMonoTime, source_ordinal]`.
Each source is selected independently as the newest valid event at or before
the absolute grid tick. An invalid carState event is dropped without replacing
the previous valid carState; an invalid carControl, controlsState, or carOutput
event invalidates that source until its next valid event. Signed steering rate
is the causal difference of consecutive zero-order-held grid steering angles,
and training never falls back from measured carOutput torque to requested
carControl torque.
It separately records training extraction version 9, trainer schema
`starpilot.neural-lateral-plant` version 7, and SHA-256 digests for both the
training extractor and trainer source.

Inference defaults to one PyTorch CPU thread because the small recurrent
workload is faster without thread-pool fan-out. Set
`COMMA_DYNAMICS_TORCH_THREADS` explicitly when benchmarking a different host.

The replay is not bit-exact. Its pure controller kernel warms the runtime-style
measurement-rate filter and phase state from all 300 history rows, starts from
the last post-update as-of integral value, models freeze and PID anti-windup,
feeds commands through the neural plant ensemble, and explicitly carries the
logged actuator-limiter gap. It does not instantiate openpilot, mutate Params,
or implement an apply-to-car path.

## Verification

```powershell
uv run --project services/comma_companion/adapters/dynamics `
  --python 3.12 --with pytest python -m pytest `
  services/comma_companion/adapters/dynamics/tests
```
