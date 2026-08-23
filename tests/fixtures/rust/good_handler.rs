//! A correct durable handler -- the checker must report nothing here.
//!
//! Same work as the bad fixture, every nondeterministic operation checkpointed.

use chrono::Utc;
use durable_lambda_core::context::DurableContext;
use uuid::Uuid;

#[durable_execution]
async fn handler(event: Value, mut ctx: DurableContext) -> Result<Value, Error> {
    // Clock and identity are checkpointed, so replay reuses the values.
    let started = ctx
        .step("started_at", || async { Ok::<_, String>(Utc::now().to_rfc3339()) })
        .await?;

    let correlation = ctx
        .step("correlation_id", || async { Ok::<_, String>(Uuid::new_v4().to_string()) })
        .await?;

    // The decision itself is checkpointed, so control flow is stable.
    let shift = ctx
        .step("pick_shift", || async {
            Ok::<_, String>(if Utc::now().hour() < 12 { "morning" } else { "afternoon" })
        })
        .await?;

    // I/O belongs inside a step -- that is what a step is for.
    let receipt = ctx
        .step("save_order", || async {
            let body = std::fs::read_to_string("/tmp/order.json")?;
            Ok::<_, String>(body)
        })
        .await?;

    // Branching on checkpointed data is fine.
    if shift == "morning" {
        ctx.step("morning_work", || async { Ok::<_, String>("am") }).await?;
    }

    // A loop suffix from a stable index, not from a clock.
    for index in 0..3 {
        ctx.step(&format!("item-{}", index), || async { Ok::<_, String>(index) })
            .await?;
    }

    Ok(json!({ "started": started, "correlation": correlation, "receipt": receipt }))
}
