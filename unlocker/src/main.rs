use std::env;
use std::fs;
use std::process;
use std::thread;
use std::time::{Duration, Instant};

use steamworks::{AppId, Client};

struct Args {
    appid: u32,
    achievement: String,
    dry_run: bool,
}

fn parse_args() -> Result<Args, String> {
    let mut appid: Option<u32> = None;
    let mut achievement: Option<String> = None;
    let mut dry_run = false;

    let args: Vec<String> = env::args().skip(1).collect();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--appid" => {
                i += 1;
                appid = args
                    .get(i)
                    .ok_or("--appid requires a value")?
                    .parse()
                    .ok();
                if appid.is_none() {
                    return Err("--appid value is not a valid integer".into());
                }
            }
            "--achievement" => {
                i += 1;
                achievement = Some(
                    args.get(i)
                        .ok_or("--achievement requires a value")?
                        .clone(),
                );
            }
            "--dry-run" => {
                dry_run = true;
            }
            "-h" | "--help" => {
                return Err("help".into());
            }
            other => return Err(format!("unknown arg: {other}")),
        }
        i += 1;
    }

    Ok(Args {
        appid: appid.ok_or("--appid required")?,
        achievement: achievement.ok_or("--achievement required")?,
        dry_run,
    })
}

fn usage() {
    eprintln!("usage: unlocker --appid APPID --achievement API_NAME [--dry-run]");
}

fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            if e != "help" {
                eprintln!("{e}");
            }
            usage();
            process::exit(2);
        }
    };

    if args.dry_run {
        println!(
            "dry-run: would unlock {} on appid {}",
            args.achievement, args.appid
        );
        return;
    }

    // chdir next to the binary so the SDK's steam_appid.txt lookup and
    // our own write both land in the same place, regardless of where the
    // caller invoked the binary from.
    if let Ok(exe) = env::current_exe() {
        if let Some(exe_dir) = exe.parent() {
            let _ = env::set_current_dir(exe_dir);
        }
    }
    if let Err(e) = fs::write("steam_appid.txt", args.appid.to_string()) {
        eprintln!("write steam_appid.txt: {e}");
        process::exit(1);
    }

    let (client, single) = match Client::init_app(AppId(args.appid)) {
        Ok(pair) => pair,
        Err(e) => {
            eprintln!("steam init failed: {e:?} (is steam running and logged in?)");
            process::exit(1);
        }
    };

    let user_stats = client.user_stats();
    user_stats.request_current_stats();

    // Pump callbacks briefly so current stats arrive before we mutate them.
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        single.run_callbacks();
        thread::sleep(Duration::from_millis(50));
    }

    let ach = user_stats.achievement(&args.achievement);
    if let Err(e) = ach.set() {
        eprintln!("set failed for {}: {e:?}", args.achievement);
        process::exit(1);
    }
    if let Err(e) = user_stats.store_stats() {
        eprintln!("store_stats failed: {e:?}");
        process::exit(1);
    }

    println!("unlocked {} on appid {}", args.achievement, args.appid);
}
