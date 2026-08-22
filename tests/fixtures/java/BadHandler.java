package lab;

import java.time.Instant;
import java.time.LocalDateTime;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import java.util.UUID;
import software.amazon.awssdk.services.dynamodb.DynamoDbClient;

/**
 * Deliberately broken durable handler in the Java SDK shape.
 *
 * Note the argument order: ctx.step(name, Type.class, lambda) -- the callback is
 * third, behind a Class token. There is also a two-argument overload, so the
 * body has to be located by kind rather than by position.
 */
public class BadHandler extends DurableHandler<Order, OrderResult> {

    private static final List<String> AUDIT = new ArrayList<>();
    private final DynamoDbClient ddb = DynamoDbClient.create();
    private final Random rng = new Random();
    private String lastReceipt;

    @Override
    protected OrderResult handleRequest(Order order, DurableContext ctx) {
        String runId = order.getId();

        // RG001 -- clock outside a step
        long startedAt = System.currentTimeMillis();

        // RG001 -- clock via java.time
        Instant stamp = Instant.now();

        // RG001 -- identity outside a step
        UUID correlationId = UUID.randomUUID();

        // RG001 -- unseeded `new Random()` reads entropy
        Random local = new Random();

        // RG001 -- method on a field whose declared type is Random
        int jitter = rng.nextInt(100);

        // RG002 -- filesystem outside a step, written inline. The same call
        // reached through a private helper is covered by the interprocedural
        // tests in test_java_frontend.py.
        String config;
        try {
            config = Files.readString(Path.of("/tmp/config.json"));
        } catch (Exception e) {
            config = "";
        }

        // RG002 -- AWS SDK client call outside a step
        ddb.putItem(builder -> builder.tableName("orders"));

        // RG004 -- branch on wall-clock time; replay may take the other path
        String shift;
        if (LocalDateTime.now().getHour() < 12) {
            shift = "morning";
        } else {
            shift = "afternoon";
        }

        List<String> receipts = new ArrayList<>();

        // RG003 -- three legal-but-wrong writes out of a step body.
        // Reassigning a captured local is absent on purpose: Java requires
        // captured locals to be effectively final, so it will not compile.
        ctx.step("save-order", String.class, stepCtx -> {
            String receipt = "receipt-" + runId;
            receipts.add(receipt);     // captured collection mutation
            AUDIT.add(runId);          // static field mutation
            this.lastReceipt = receipt; // instance field write
            return receipt;
        });

        // RG005 -- step name built from the clock cannot be matched on resume
        ctx.step("process-" + System.currentTimeMillis(), String.class, stepCtx -> "work");

        // RG900 -- method reference body, not resolvable without interprocedural analysis
        ctx.step("external", String.class, this::externalBody);

        return new OrderResult(runId, shift, jitter, startedAt, stamp, correlationId, config);
    }

    private String externalBody(Object stepCtx) {
        return "external";
    }
}
