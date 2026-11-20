# Learned lateral torque controller

Offline pipeline behind the Ioniq 5 "Learned Controller" choice in
Steering Controller. It learns a model of the car's steering from recorded
drives, trains a torque law inside that model, and checks the result by
driving the real controller code against it.

The runtime half lives in `openpilot/starpilot/lateral/`:

- `learned_torque_core.py`: the torque law itself, numpy only, no openpilot
  imports. The trainer and the simulator import this same file.
- `learned_torque_policy.py`: wraps the StarPilot vehicle policy. That policy
  still steers alone below 3 m/s, is blended out by 6 m/s, and takes over for
  the rest of the drive if the learned law raises or returns a non-finite torque.
- `learned_torque_ioniq5.npz`: the trained weights.

## How it works

**Plant.** `plant.py` is an ensemble of five small GRUs. Each step takes the
applied torque, wheel angle and rate, speed, acceleration and road roll, and
returns the change in wheel angle 10 ms later. Lateral acceleration is then
computed with the same vehicle-model geometry the controller uses. Training
teacher-forces half a second and then predicts three seconds open loop from the
applied torque alone.

**Policy.** `train_policy.py` replays thousands of recorded situations in
parallel, hands the wheel to the policy, and differentiates a tracking and
smoothness cost through the whole closed loop. Each rollout draws its own
ensemble member, torque gain (0.7 to 1.4), torque bias (+/-0.05), actuation lag
(0 to 5 frames) and planner delay (0.25 to 0.42 s), and the policy sees the
wheel angle in the sensor's 0.1 degree steps.

**Controller form.** The law outputs a bounded change in torque per frame and
is clamped to the car's own rate and driver-torque limits, so the command is
continuous by construction and what it asks for is what the car applies.

## Reproducing

Training needs CUDA PyTorch; `simulate.py` and `extract_rlogs.py` need the
openpilot Python stack (WSL works). Paths below are examples.

```sh
# 1. Per-route 100 Hz corpus from Comma Companion telemetry (slow share: hours)
python extract_corpus.py --output D:/lat/corpus
#    ...or from rlogs already staged locally, same output format
python extract_rlogs.py --root /mnt/g/training_data/starpilot-20inch-ioniq5 --output /mnt/d/lat/corpus

# 2. One table of clean engaged driving, split by route
python dataset.py --corpus D:/lat/corpus --output D:/lat/dataset.npz

# 3. Plant ensemble, then a causal sanity check of its torque response
python train_plant.py --dataset D:/lat/dataset.npz --output D:/lat/plant/plant.pt
python plant_response.py --dataset D:/lat/dataset.npz --plant D:/lat/plant/plant.pt

# 4. Policy, and proof that the trained and deployed controllers are the same
python train_policy.py --dataset D:/lat/dataset.npz --plant D:/lat/plant/plant.pt --output D:/lat/policy/policy.pt
python check_parity.py --dataset D:/lat/dataset.npz --plant D:/lat/plant/plant.pt --policy D:/lat/policy/policy.pt

# 5. Install the weights and compare the real controllers in closed loop
cp D:/lat/policy/policy.npz openpilot/starpilot/lateral/learned_torque_ioniq5.npz
python simulate.py --dataset /mnt/d/lat/dataset.npz --plant /mnt/d/lat/plant/plant.npz --stress
```

`simulate.py` builds `LatControlTorque` through the same factory controlsd
uses and applies `apply_driver_steer_torque_limits` as the car controller
does, so its "starpilot" and "learned" rows are the shipped code, not a model
of it.

## What the simulator can and cannot tell you

It replays the planner's recorded requests, so it cannot show how the driving
model would re-plan around a controller that tracks differently. The plant is
fitted to engaged driving above about 4 m/s under the controllers that were
running at the time; behaviour far from that (very low speed, limit handling,
strong driver input) is extrapolation. Treat a simulator win as the reason to
try a controller on the road, not as the result.
