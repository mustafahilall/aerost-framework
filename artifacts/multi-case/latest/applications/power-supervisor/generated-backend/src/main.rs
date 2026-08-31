#![forbid(unsafe_code)]
use std::env;
use std::fs;
use std::hint::black_box;
use std::time::Instant;
use aerost_generated_power_supervisor::*;

fn parse_supervisor_state(value: &str) -> SupervisorState {
    match value {
        "INITIALIZING" => SupervisorState::Initializing,
        "NORMAL" => SupervisorState::Normal,
        "ENERGY_CONSERVATION" => SupervisorState::EnergyConservation,
        "DEGRADED" => SupervisorState::Degraded,
        "CRITICAL" => SupervisorState::Critical,
        "RECOVERY_PENDING" => SupervisorState::RecoveryPending,
        "LOCKOUT" => SupervisorState::Lockout,
        _ => panic!("invalid SupervisorState value: {value}"),
    }
}

fn format_supervisor_state(value: SupervisorState) -> &'static str {
    match value {
        SupervisorState::Initializing => "INITIALIZING",
        SupervisorState::Normal => "NORMAL",
        SupervisorState::EnergyConservation => "ENERGY_CONSERVATION",
        SupervisorState::Degraded => "DEGRADED",
        SupervisorState::Critical => "CRITICAL",
        SupervisorState::RecoveryPending => "RECOVERY_PENDING",
        SupervisorState::Lockout => "LOCKOUT",
    }
}

fn parse_bool(value: &str) -> bool { match value { "1" | "true" | "TRUE" => true, "0" | "false" | "FALSE" => false, _ => panic!("invalid bool: {value}") } }
fn bool01(value: bool) -> &'static str { if value { "1" } else { "0" } }

#[derive(Clone)]
struct ProtocolCase { scenario: String, cycle: u64, retained: Retained, inputs: Inputs, runtime_fault: bool }

fn parse_protocol(path: &str) -> Vec<ProtocolCase> {
    let content = fs::read_to_string(path).expect("read protocol file");
    let mut scenario = String::new();
    let mut retained = Retained::default();
    let mut cases: Vec<ProtocolCase> = Vec::new();
    for raw in content.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') { continue; }
        let parts: Vec<&str> = line.split('|').collect();
        match parts[0] {
            "S" => {
                scenario = parts[1].to_string();
                retained = Retained::default();
                for part in &parts[2..] {
                    let (key, value) = part.split_once('=').expect("state key=value");
                    match key {
                        "State" => retained.state = parse_supervisor_state(value),
                        "BlockingFaultLatched" => retained.blocking_fault_latched = parse_bool(value),
                        _ => panic!("unknown retained field: {key}"),
                    }
                }
            }
            "C" => {
                let cycle: u64 = parts[1].parse().expect("cycle");
                let mut inputs = Inputs::default();
                let mut runtime_fault = false;
                for part in &parts[2..] {
                    let (key, value) = part.split_once('=').expect("input key=value");
                    match key {
                        "InputSetValid" => inputs.input_set_valid = parse_bool(value),
                        "InitializationComplete" => inputs.initialization_complete = parse_bool(value),
                        "BatteryAHealthy" => inputs.battery_a_healthy = parse_bool(value),
                        "BatteryBHealthy" => inputs.battery_b_healthy = parse_bool(value),
                        "EssentialPowerAvailable" => inputs.essential_power_available = parse_bool(value),
                        "MainBusWarningPersisted" => inputs.main_bus_warning_persisted = parse_bool(value),
                        "MainBusCritical" => inputs.main_bus_critical = parse_bool(value),
                        "ReserveEnergyLow" => inputs.reserve_energy_low = parse_bool(value),
                        "ReturnEnergyInsufficient" => inputs.return_energy_insufficient = parse_bool(value),
                        "ContactorMismatchPersisted" => inputs.contactor_mismatch_persisted = parse_bool(value),
                        "RecoveryStable" => inputs.recovery_stable = parse_bool(value),
                        "ResetAuthorized" => inputs.reset_authorized = parse_bool(value),
                        "PayloadOvercurrent" => inputs.payload_overcurrent = parse_bool(value),
                        "FlightPhaseLanding" => inputs.flight_phase_landing = parse_bool(value),
                        "BlockingRuntimeFault" => runtime_fault = parse_bool(value),
                        _ => panic!("unknown input field: {key}"),
                    }
                }
                cases.push(ProtocolCase { scenario: scenario.clone(), cycle, retained: retained.clone(), inputs: inputs.clone(), runtime_fault });
                retained = step(retained, &inputs, runtime_fault).retained;
            }
            _ => panic!("unknown protocol record: {}", parts[0]),
        }
    }
    cases
}

fn run_file(path: &str) {
    for case in parse_protocol(path) {
        let result = step(case.retained, &case.inputs, case.runtime_fault);
        let statements = result.trace.statements.join(",");
        let cases = result.trace.case_arms.join(",");
        let diagnostics = result.trace.diagnostics.join(",");
        let decisions = result.trace.decisions.iter().map(|d| {
            let c = d.conditions.iter().map(|(id,v)| format!("{}={}", id, bool01(*v))).collect::<Vec<_>>().join(",");
            format!("{}:{}:{}", d.id, bool01(d.result), c)
        }).collect::<Vec<_>>().join(";");
        let mut fields: Vec<String> = Vec::new();
        fields.push(format!("R|{}|{}", case.scenario, case.cycle));
        fields.push(format!("State={}", format_supervisor_state(result.retained.state).to_string()));
        fields.push(format!("BlockingFaultLatched={}", bool01(result.retained.blocking_fault_latched).to_string()));
        fields.push(format!("PayloadPermit={}", bool01(result.outputs.payload_permit).to_string()));
        fields.push(format!("ShedNonessentialLoads={}", bool01(result.outputs.shed_nonessential_loads).to_string()));
        fields.push(format!("IsolateBatteryA={}", bool01(result.outputs.isolate_battery_a).to_string()));
        fields.push(format!("IsolateBatteryB={}", bool01(result.outputs.isolate_battery_b).to_string()));
        fields.push(format!("ReturnToHomeRequest={}", bool01(result.outputs.return_to_home_request).to_string()));
        fields.push(format!("ImmediateLandingRequest={}", bool01(result.outputs.immediate_landing_request).to_string()));
        fields.push(format!("MissionProgressionPermit={}", bool01(result.outputs.mission_progression_permit).to_string()));
        fields.push(format!("BlockingPowerFault={}", bool01(result.outputs.blocking_power_fault).to_string()));
        fields.push(format!("SupervisorState={}", format_supervisor_state(result.outputs.supervisor_state).to_string()));
        fields.push(format!("NormalCommitInhibited={}", bool01(result.normal_commit_inhibited)));
        fields.push(format!("Statements={}", statements));
        fields.push(format!("Cases={}", cases));
        fields.push(format!("Diagnostics={}", diagnostics));
        fields.push(format!("Decisions={}", decisions));
        println!("{}", fields.join("|"));
    }
}

fn summarize(values: &mut [u128]) -> (u128, u128, f64, u128, u128, u128) {
    values.sort_unstable();
    let count = values.len();
    let sum: u128 = values.iter().sum();
    let idx = |p: f64| -> usize { (((count - 1) as f64) * p).round() as usize };
    (values[0], values[count / 2], sum as f64 / count as f64, values[idx(0.95)], values[idx(0.99)], values[count - 1])
}

fn benchmark_file(path: &str, samples_per_path: usize) {
    assert!(samples_per_path > 0, "samples per path must be positive");
    let cases = parse_protocol(path);
    assert!(!cases.is_empty(), "protocol contains no controlled cycles");
    let warmup_per_path = 1_000usize;
    let mut global_values: Vec<u128> = Vec::with_capacity(cases.len() * samples_per_path);
    let mut worst_scenario = String::new();
    let mut worst_cycle = 0u64;
    let mut worst_maximum = 0u128;
    for case in &cases {
        for _ in 0..warmup_per_path { black_box(step(case.retained.clone(), black_box(&case.inputs), case.runtime_fault)); }
        let mut values: Vec<u128> = Vec::with_capacity(samples_per_path);
        for _ in 0..samples_per_path {
            let start = Instant::now();
            black_box(step(case.retained.clone(), black_box(&case.inputs), case.runtime_fault));
            values.push(start.elapsed().as_nanos());
        }
        let (minimum, median, mean, p95, p99, maximum) = summarize(&mut values);
        if maximum >= worst_maximum { worst_maximum = maximum; worst_scenario = case.scenario.clone(); worst_cycle = case.cycle; }
        global_values.extend(values.iter().copied());
        println!("BENCHCASE|{}|{}|{}|{}|{}|{:.2}|{}|{}|{}", case.scenario, case.cycle, samples_per_path, minimum, median, mean, p95, p99, maximum);
    }
    let total_samples = global_values.len();
    let (minimum, median, mean, p95, p99, maximum) = summarize(&mut global_values);
    println!("BENCHTOTAL|{}|{}|{}|{}|{}|{}|{:.2}|{}|{}|{}|{}|{}|{}", cases.len(), samples_per_path, warmup_per_path, total_samples, minimum, median, mean, p95, p99, maximum, worst_scenario, worst_cycle, worst_maximum);
}

fn main() {
    let args: Vec<String> = env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("run") => run_file(args.get(2).expect("protocol file")),
        Some("benchmark-protocol") => benchmark_file(args.get(2).expect("protocol file"), args.get(3).and_then(|x| x.parse().ok()).unwrap_or(100_000)),
        _ => panic!("usage: generated-backend run <protocol-file> | benchmark-protocol <protocol-file> [samples-per-path]"),
    }
}
