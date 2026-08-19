/**
 * A correct durable handler — the checker must report nothing here.
 *
 * Same work as the bad fixture, every nondeterministic operation checkpointed.
 */

import { withDurableExecution, DurableContext } from "@aws/durable-execution-sdk-js";
import { DynamoDBClient, PutItemCommand } from "@aws-sdk/client-dynamodb";

const ddb = new DynamoDBClient({});

const handler = async (event: any, context: DurableContext) => {
  const runId = event.runId;

  // Clock and identity are checkpointed, so replay reuses the original values.
  const startedAt = await context.step("started-at", async () => Date.now());
  const correlationId = await context.step("correlation-id", async () =>
    crypto.randomUUID(),
  );

  // The decision itself is checkpointed, so control flow is stable.
  const shift = await context.step("pick-shift", async () =>
    new Date().getHours() < 12 ? "morning" : "afternoon",
  );

  // I/O belongs inside a step — this is what steps are for.
  const receiptId = await context.step("save-order", async () => {
    await ddb.send(new PutItemCommand({ TableName: "orders" }));
    return `receipt-${runId}`;
  });

  // Values come back through return values and are assigned in the handler,
  // where replay rebuilds them from the checkpointed result.
  const receipts: string[] = [];
  receipts.push(receiptId);

  // Branching on checkpointed data is fine.
  if (shift === "morning") {
    await context.step("morning-work", async () => "morning");
  } else {
    await context.step("afternoon-work", async () => "afternoon");
  }

  // A loop suffix derived from a stable index, not from a clock.
  for (const [index, item] of (event.items ?? []).entries()) {
    await context.step(`item-${index}`, async () => String(item));
  }

  return { runId, correlationId, startedAt, receipts };
};

export const lambdaHandler = withDurableExecution(handler);
