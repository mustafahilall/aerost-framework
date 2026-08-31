#![forbid(unsafe_code)]
use std::env;
use std::fs;
use std::hint::black_box;
use std::time::Instant;
use aerost_generated_communication_link_supervisor::*;

fn parse_communication_link_state(value: &str) -> CommunicationLinkState {
    match value {
        "INITIALIZING" => CommunicationLinkState::Initializing,
        "NOMINAL" => CommunicationLinkState::Nominal,
        "DEGRADED" => CommunicationLinkState::Degraded,
        "LOST_LINK" => CommunicationLinkState::LostLink,
        "RECOVERY_PENDING" => CommunicationLinkState::RecoveryPending,
        "LOCKOUT" => CommunicationLinkState::Lockout,
        _ => panic!("invalid CommunicationLinkState value: {value}"),
    }
}

fn format_communication_link_state(value: CommunicationLinkState) -> &'static str {
    match value {
        CommunicationLinkState::Initializing => "INITIALIZING",
        CommunicationLinkState::Nominal => "NOMINAL",
        CommunicationLinkState::Degraded => "DEGRADED",
        CommunicationLinkState::LostLink => "LOST_LINK",
        CommunicationLinkState::RecoveryPending => "RECOVERY_PENDING",
        CommunicationLinkState::Lockout => "LOCKOUT",
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
                        "State" => retained.state = parse_communication_link_state(value),
                        "LinkFaultLatched" => retained.link_fault_latched = parse_bool(value),
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
                        "PrimaryLinkAvailable" => inputs.primary_link_available = parse_bool(value),
                        "SecondaryLinkAvailable" => inputs.secondary_link_available = parse_bool(value),
                        "LinkQualityDegraded" => inputs.link_quality_degraded = parse_bool(value),
                        "LinkLossPersisted" => inputs.link_loss_persisted = parse_bool(value),
                        "RecoveryStable" => inputs.recovery_stable = parse_bool(value),
                        "ResetAuthorized" => inputs.reset_authorized = parse_bool(value),
                        "CommandChannelAuthenticated" => inputs.command_channel_authenticated = parse_bool(value),
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
        fields.push(format!("State={}", format_communication_link_state(result.retained.state).to_string()));
        fields.push(format!("LinkFaultLatched={}", bool01(result.retained.link_fault_latched).to_string()));
        fields.push(format!("CommandPermit={}", bool01(result.outputs.command_permit).to_string()));
        fields.push(format!("UsePrimaryLink={}", bool01(result.outputs.use_primary_link).to_string()));
        fields.push(format!("UseSecondaryLink={}", bool01(result.outputs.use_secondary_link).to_string()));
        fields.push(format!("DegradedLinkIndication={}", bool01(result.outputs.degraded_link_indication).to_string()));
        fields.push(format!("LostLinkProcedureRequest={}", bool01(result.outputs.lost_link_procedure_request).to_string()));
        fields.push(format!("ReturnToHomeRequest={}", bool01(result.outputs.return_to_home_request).to_string()));
        fields.push(format!("MissionProgressionPermit={}", bool01(result.outputs.mission_progression_permit).to_string()));
        fields.push(format!("BlockingLinkFault={}", bool01(result.outputs.blocking_link_fault).to_string()));
        fields.push(format!("SupervisorState={}", format_communication_link_state(result.outputs.supervisor_state).to_string()));
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
