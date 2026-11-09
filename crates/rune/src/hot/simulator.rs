//! The simulator shares a filesystem with the Mac that compiled the patch. Its
//! sandbox temporary directory carries one ordered patch at a time and its ack.

use std::{
    fs, io,
    path::{Path, PathBuf},
    sync::{
        OnceLock,
        atomic::{AtomicU64, Ordering},
    },
    thread,
    time::Duration,
};

use serde::Deserialize;
use serde_json::json;

struct Mailbox {
    path: PathBuf,
    pid: u32,
    aslr: usize,
    last_presented: AtomicU64,
}

#[derive(Deserialize)]
struct Patch {
    pid: u32,
    aslr: usize,
    generation: u64,
    table: subsecond::JumpTable,
}

static MAILBOX: OnceLock<Mailbox> = OnceLock::new();

pub(super) fn start() -> bool {
    let Ok(token) = std::env::var("RUNE_IOS_HOT") else {
        return false;
    };
    if token.len() != 32 || !token.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        eprintln!("rune-hot: invalid simulator session token");
        return true;
    }
    let path = std::env::temp_dir().join(format!("rune-hot-{token}"));
    let pid = std::process::id();
    // Subsecond initializes its executable reference the first time it is read.
    let aslr = subsecond::aslr_reference();
    if aslr == 0 {
        eprintln!(
            "rune-hot: the simulator executable must export main (use dx build --fat-binary)"
        );
        return true;
    }
    let mailbox = MAILBOX.get_or_init(|| Mailbox {
        path,
        pid,
        aslr,
        last_presented: AtomicU64::new(0),
    });
    if let Err(error) = fs::create_dir_all(&mailbox.path).and_then(|()| {
        write_json(
            &mailbox.path.join("ready.json"),
            json!({ "pid": pid, "aslr": aslr }),
        )
    }) {
        eprintln!("rune-hot: simulator mailbox: {error}");
        return true;
    }
    if let Err(error) = thread::Builder::new()
        .name("rune-ios-hot".into())
        .spawn(move || listen(mailbox))
    {
        eprintln!("rune-hot: simulator patch listener: {error}");
    }
    true
}

fn listen(mailbox: &Mailbox) {
    let request = mailbox.path.join("patch.json");
    let processing = mailbox.path.join("applying.json");
    let mut applied = 0;
    loop {
        match fs::rename(&request, &processing) {
            Ok(()) => {
                let bytes = fs::read(&processing);
                let _ = fs::remove_file(&processing);
                match bytes.and_then(|bytes| {
                    serde_json::from_slice::<Patch>(&bytes).map_err(io::Error::other)
                }) {
                    Ok(patch) => match apply(mailbox, patch, applied) {
                        Ok(()) => applied += 1,
                        Err(error) => {
                            let _ = write_json(
                                &mailbox.path.join("error.json"),
                                json!({
                                    "generation": applied + 1, "error": error
                                }),
                            );
                        }
                    },
                    Err(error) => eprintln!("rune-hot: invalid simulator patch request: {error}"),
                }
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                if !mailbox.path.exists() {
                    return;
                }
            }
            Err(error) => {
                eprintln!("rune-hot: simulator mailbox stopped: {error}");
                return;
            }
        }
        thread::sleep(Duration::from_millis(25));
    }
}

fn apply(mailbox: &Mailbox, patch: Patch, applied: u64) -> Result<(), String> {
    if patch.pid != mailbox.pid || patch.aslr != mailbox.aslr || patch.generation != applied + 1 {
        return Err("patch belongs to a different simulator process or generation".into());
    }
    let mut table = patch.table;
    let name = format!("patch-{}.dylib", patch.generation);
    if table.lib != Path::new(&name) {
        return Err("unexpected simulator patch library name".into());
    }
    table.lib = mailbox.path.join(name);
    // SAFETY: the local guarded launcher built the table against this process's
    // executable and ASLR reference. The mailbox and dylib stay in its sandbox.
    match std::panic::catch_unwind(|| unsafe { subsecond::apply_patch(table) }) {
        Ok(Ok(())) => Ok(()),
        Ok(Err(error)) => Err(error.to_string()),
        Err(_) => Err("Subsecond panicked while loading the patch".into()),
    }
}

pub(super) fn presented(generation: u64) {
    let Some(mailbox) = MAILBOX.get() else { return };
    if generation <= mailbox.last_presented.load(Ordering::Relaxed) {
        return;
    }
    match write_json(
        &mailbox.path.join("presented.json"),
        json!({
            "pid": mailbox.pid, "generation": generation
        }),
    ) {
        Ok(()) => mailbox.last_presented.store(generation, Ordering::Relaxed),
        Err(error) => eprintln!("rune-hot: could not acknowledge simulator patch: {error}"),
    }
}

fn write_json(path: &Path, data: serde_json::Value) -> io::Result<()> {
    let temp = path.with_extension("tmp");
    fs::write(&temp, serde_json::to_vec(&data)?)?;
    fs::rename(temp, path)
}
