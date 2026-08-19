/**
 * Deliberately broken durable handler in the JS/TS SDK shape.
 *
 * Note the argument order: `context.step(name, callback)` — the reverse of the
 * Python SDK. Same bugs, different surface.
 */

import { withDurableExecution, DurableContext } from "@aws/durable-execution-sdk-js";
import { DynamoDBClient, PutItemCommand } from "@aws-sdk/client-dynamodb";

const ddb = new DynamoDBClient({});
const AUDIT: string[] = [];

const handler = async (event: any, context: DurableContext) => {
  const runId = event.runId;

  // RG001 — clock outside a step
  const startedAt = Date.now();

  // RG001 — zero-arg `new Date()` reads the clock
  const stamp = new Date();

  // RG001 — random outside a step
  const jitter = Math.random();

  // RG001 — identity outside a step
  const correlationId = crypto.randomUUID();

  // RG002 — network call outside a step, repeated on every replay
  const profile = await fetch(`https://example.internal/profile/${runId}`);

  // RG002 — AWS SDK call outside a step
  await ddb.send(new PutItemCommand({ TableName: "orders" }));

  // RG004 — branch on wall-clock time; replay may take the other path
  let shift: string;
  if (new Date().getHours() < 12) {
    shift = "morning";
  } else {
    shift = "afternoon";
  }

  const receipts: string[] = [];
  let lastReceipt = "";
  const state: Record<string, boolean> = {};

  // RG003 — three flavours of writing to state the step body does not own
  await context.step("save-order", async (s) => {
    const receipt = await saveOrder(runId);
    receipts.push(receipt);      // captured array mutation
    AUDIT.push(runId);           // module-level mutation
    lastReceipt = receipt;       // bare assignment writes through in JS
    state["done"] = true;        // subscript write
    return receipt;
  });

  // RG005 — step name built from the clock cannot be matched on resume
  await context.step(`process-${Date.now()}`, async (s) => "work");

  // RG900 — step body passed as an unresolvable reference
  await context.step("external", externalBody);

  return { runId, shift, jitter, correlationId, startedAt, stamp, profile };
};

export const lambdaHandler = withDurableExecution(handler);
