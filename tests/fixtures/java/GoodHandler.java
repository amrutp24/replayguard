package lab;

import java.time.Instant;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import software.amazon.awssdk.services.dynamodb.DynamoDbClient;

/**
 * A correct durable handler -- the checker must report nothing here.
 *
 * Same work as the bad fixture, every nondeterministic operation checkpointed.
 */
public class GoodHandler extends DurableHandler<Order, OrderResult> {

    private final DynamoDbClient ddb = DynamoDbClient.create();

    @Override
    protected OrderResult handleRequest(Order order, DurableContext ctx) {
        String runId = order.getId();

        // Clock and identity are checkpointed, so replay reuses the values.
        Long startedAt = ctx.step("started-at", Long.class,
                stepCtx -> System.currentTimeMillis());

        String correlationId = ctx.step("correlation-id", String.class,
                stepCtx -> UUID.randomUUID().toString());

        // The decision itself is checkpointed, so control flow is stable.
        String shift = ctx.step("pick-shift", String.class,
                stepCtx -> LocalDateTime.now().getHour() < 12 ? "morning" : "afternoon");

        // I/O belongs inside a step -- this is what steps are for.
        String receiptId = ctx.step("save-order", String.class, stepCtx -> {
            ddb.putItem(builder -> builder.tableName("orders"));
            return "receipt-" + runId;
        });

        // Values come back through return values and are applied in the handler,
        // where replay rebuilds them from the checkpointed result.
        List<String> receipts = new ArrayList<>();
        receipts.add(receiptId);

        // Branching on checkpointed data is fine.
        if (shift.equals("morning")) {
            ctx.step("morning-work", String.class, stepCtx -> "morning");
        } else {
            ctx.step("afternoon-work", String.class, stepCtx -> "afternoon");
        }

        // A loop suffix derived from a stable index, not from a clock.
        for (int index = 0; index < order.getItems().size(); index++) {
            final int i = index;
            ctx.step("item-" + index, String.class, stepCtx -> render(i));
        }

        // The two-argument overload, without the Class token.
        ctx.step("finalise", stepCtx -> "done");

        return new OrderResult(runId, shift, startedAt, correlationId, receipts);
    }

    private String render(int index) {
        return "item-" + index;
    }
}
