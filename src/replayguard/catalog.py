"""Catalog of nondeterministic and side-effecting symbols, per language.

Sourced from AWS's own determinism rules for durable execution, which name
wall-clock time, random sources, external services, the local file system, and
mutable global state as the things that must not run outside a durable
operation.

Kept as data rather than scattered through the rules so that adding a symbol is
a one-line change and so the same categories mean the same thing in every
language frontend.
"""

from __future__ import annotations

from enum import Enum

from .ir import Language


class Category(str, Enum):
    CLOCK = "clock"
    RANDOM = "random"
    IDENTITY = "identity"
    NETWORK = "network"
    FILESYSTEM = "filesystem"
    PROCESS = "process"

    @property
    def why(self) -> str:
        return {
            "clock": "wall-clock time differs between the original run and the replay",
            "random": "a random source yields different values on replay",
            "identity": "generated identifiers differ on replay, so downstream "
            "steps receive different inputs",
            "network": "external calls re-execute on every replay, duplicating "
            "side effects and returning different results",
            "filesystem": "the execution environment is not the same machine on "
            "resume, so local file state does not survive",
            "process": "process-level state is not stable across replay slices",
        }[self.value]


#: Canonical dotted names. Frontends resolve import aliases to these before
#: lookup, so `import time as t; t.time()` still matches `time.time`.
_PYTHON: dict[str, Category] = {
    # clock
    "time.time": Category.CLOCK,
    "time.monotonic": Category.CLOCK,
    "time.perf_counter": Category.CLOCK,
    "time.localtime": Category.CLOCK,
    "time.gmtime": Category.CLOCK,
    "datetime.datetime.now": Category.CLOCK,
    "datetime.datetime.utcnow": Category.CLOCK,
    "datetime.datetime.today": Category.CLOCK,
    "datetime.date.today": Category.CLOCK,
    # random
    "random.random": Category.RANDOM,
    "random.randint": Category.RANDOM,
    "random.choice": Category.RANDOM,
    "random.shuffle": Category.RANDOM,
    "random.uniform": Category.RANDOM,
    "random.sample": Category.RANDOM,
    "secrets.token_hex": Category.RANDOM,
    "secrets.token_bytes": Category.RANDOM,
    "secrets.token_urlsafe": Category.RANDOM,
    "secrets.choice": Category.RANDOM,
    "os.urandom": Category.RANDOM,
    "numpy.random.rand": Category.RANDOM,
    "numpy.random.randn": Category.RANDOM,
    # identity
    "uuid.uuid1": Category.IDENTITY,
    "uuid.uuid4": Category.IDENTITY,
    # network / external services
    "boto3.client": Category.NETWORK,
    "boto3.resource": Category.NETWORK,
    "requests.get": Category.NETWORK,
    "requests.post": Category.NETWORK,
    "requests.put": Category.NETWORK,
    "requests.delete": Category.NETWORK,
    "requests.request": Category.NETWORK,
    "httpx.get": Category.NETWORK,
    "httpx.post": Category.NETWORK,
    "urllib.request.urlopen": Category.NETWORK,
    "socket.socket": Category.NETWORK,
    # filesystem
    "open": Category.FILESYSTEM,
    "os.remove": Category.FILESYSTEM,
    "os.mkdir": Category.FILESYSTEM,
    "os.listdir": Category.FILESYSTEM,
    "shutil.copy": Category.FILESYSTEM,
    "shutil.rmtree": Category.FILESYSTEM,
    "pathlib.Path.write_text": Category.FILESYSTEM,
    "pathlib.Path.read_text": Category.FILESYSTEM,
    # process
    "os.getpid": Category.PROCESS,
    "threading.current_thread": Category.PROCESS,
}

_TYPESCRIPT: dict[str, Category] = {
    # clock
    "Date.now": Category.CLOCK,
    "Date": Category.CLOCK,  # `new Date()` with no arguments
    "performance.now": Category.CLOCK,
    "process.hrtime": Category.CLOCK,
    "process.uptime": Category.CLOCK,
    # random
    "Math.random": Category.RANDOM,
    "crypto.getRandomValues": Category.RANDOM,
    "crypto.randomBytes": Category.RANDOM,
    "crypto.randomInt": Category.RANDOM,
    # identity
    "crypto.randomUUID": Category.IDENTITY,
    "uuid.v1": Category.IDENTITY,
    "uuid.v4": Category.IDENTITY,
    "uuidv4": Category.IDENTITY,
    "nanoid": Category.IDENTITY,
    # network
    "fetch": Category.NETWORK,
    "axios.get": Category.NETWORK,
    "axios.post": Category.NETWORK,
    "axios.put": Category.NETWORK,
    "axios.delete": Category.NETWORK,
    "axios.request": Category.NETWORK,
    "XMLHttpRequest": Category.NETWORK,
    # filesystem
    "fs.readFile": Category.FILESYSTEM,
    "fs.readFileSync": Category.FILESYSTEM,
    "fs.writeFile": Category.FILESYSTEM,
    "fs.writeFileSync": Category.FILESYSTEM,
    "fs.promises.readFile": Category.FILESYSTEM,
    "fs.promises.writeFile": Category.FILESYSTEM,
    # process
    "process.pid": Category.PROCESS,
}

_JAVA: dict[str, Category] = {
    # clock
    "System.currentTimeMillis": Category.CLOCK,
    "System.nanoTime": Category.CLOCK,
    "Instant.now": Category.CLOCK,
    "LocalDate.now": Category.CLOCK,
    "LocalTime.now": Category.CLOCK,
    "LocalDateTime.now": Category.CLOCK,
    "ZonedDateTime.now": Category.CLOCK,
    "OffsetDateTime.now": Category.CLOCK,
    "Calendar.getInstance": Category.CLOCK,
    "Clock.systemUTC": Category.CLOCK,
    "Clock.systemDefaultZone": Category.CLOCK,
    "Date": Category.CLOCK,  # `new Date()` with no arguments
    # random
    "Math.random": Category.RANDOM,
    "Random": Category.RANDOM,  # `new Random()` with no seed
    "SecureRandom": Category.RANDOM,
    "ThreadLocalRandom.current": Category.RANDOM,
    # identity
    "UUID.randomUUID": Category.IDENTITY,
    # network
    "HttpClient.newHttpClient": Category.NETWORK,
    "URL.openConnection": Category.NETWORK,
    "URL.openStream": Category.NETWORK,
    "Socket": Category.NETWORK,
    # filesystem
    "Files.readAllBytes": Category.FILESYSTEM,
    "Files.readString": Category.FILESYSTEM,
    "Files.readAllLines": Category.FILESYSTEM,
    "Files.write": Category.FILESYSTEM,
    "Files.writeString": Category.FILESYSTEM,
    "Files.delete": Category.FILESYSTEM,
    "Files.createFile": Category.FILESYSTEM,
    "Files.newBufferedReader": Category.FILESYSTEM,
    "FileInputStream": Category.FILESYSTEM,
    "FileOutputStream": Category.FILESYSTEM,
    "FileReader": Category.FILESYSTEM,
    "FileWriter": Category.FILESYSTEM,
    # process
    "Thread.currentThread": Category.PROCESS,
    "ProcessHandle.current": Category.PROCESS,
}

#: Declared types whose instances are nondeterministic wholesale. `Random r`
#: means every `r.nextInt()` is a random source, and the method names are too
#: numerous to catalogue individually.
JAVA_TAINTED_TYPES: dict[str, Category] = {
    "Random": Category.RANDOM,
    "SecureRandom": Category.RANDOM,
    "ThreadLocalRandom": Category.RANDOM,
    "HttpClient": Category.NETWORK,
    "Socket": Category.NETWORK,
    "URLConnection": Category.NETWORK,
    "HttpURLConnection": Category.NETWORK,
}


_BY_LANGUAGE: dict[Language, dict[str, Category]] = {
    Language.PYTHON: _PYTHON,
    Language.TYPESCRIPT: _TYPESCRIPT,
    Language.JAVA: _JAVA,
}


def lookup(language: Language, dotted: str) -> Category | None:
    """Exact-match a dotted name against the catalog for a language."""
    return _BY_LANGUAGE[language].get(dotted)


def is_aws_sdk_call(language: Language, dotted: str) -> bool:
    """Heuristic for AWS SDK usage, which is too varied to enumerate.

    Client method names are unbounded (`put_item`, `send`, `invoke_model`, ...),
    so the catalog can't list them. Matching the construction and the send path
    covers the realistic cases without trying to model the whole SDK surface.
    """
    if language is Language.PYTHON:
        return dotted.startswith(("boto3.", "botocore."))
    if language is Language.JAVA:
        return dotted.startswith("software.amazon.awssdk")
    return dotted.startswith("@aws-sdk/") or dotted.endswith(".send")


def categories_for(language: Language) -> dict[str, Category]:
    return dict(_BY_LANGUAGE[language])
