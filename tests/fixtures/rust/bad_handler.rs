//! Deliberately broken durable handler, in the durable-rust closure style.
//!
//! Note what is *absent*: capturing `&mut` state in a step closure. The SDK's
//! `Send + 'static` bound means that does not compile, so the whole shape that
//! JavaScript makes easy is rejected by the borrow checker before this tool
//! ever sees it. What remains is interior mutability through a shared handle.

use chrono::Utc;
use durable_lambda_core::context::DurableContext;
use std::sync::{Arc, Mutex};
use std::time::SystemTime;
use uuid::Uuid;

static mut AUDIT_COUNT: u64 = 0;

#[durable_execution]
async fn handler(event: Value, mut ctx: DurableContext) -> Result<Value, Error> {
    // RG001 -- clock outside a step
    let started = SystemTime::now();

    // RG001 -- clock via chrono, written with the crate prefix
    let stamp = chrono::Utc::now();

    // RG001 -- identity outside a step
    let correlation = Uuid::new_v4();

    // RG002 -- filesystem outside a step
    let config = std::fs::read_to_string("/tmp/config.json")?;

    // RG004 -- branch on wall-clock time
    let shift = if Utc::now().hour() < 12 { "morning" } else { "afternoon" };

    let receipts = Arc::new(Mutex::new(Vec::new()));
    let receipts_for_step = Arc::clone(&receipts);

    // RG003 -- interior mutability through a shared handle, lost on replay
    ctx.step("save_order", move || {
        let receipts = Arc::clone(&receipts_for_step);
        async move {
            receipts.lock().unwrap().push("receipt-1".to_string());
            Ok::<_, String>("saved")
        }
    })
    .await?;

    // RG005 -- operation name built from the clock
    ctx.step(&format!("process-{}", Utc::now()), || async { Ok::<_, String>(1) })
        .await?;

    // Read back outside the step: this is what makes the write above a lost
    // update rather than a harmless write-only log.
    let count = receipts.lock().unwrap().len();

    Ok(json!({ "shift": shift, "count": count, "started": started,
               "stamp": stamp, "correlation": correlation, "config": config }))
}
