import environ

env = environ.FileAwareEnv()
IQUERY_PROBE_ITERATIONS = env.int("IQUERY_PROBE_ITERATIONS", default=8)
IQUERY_PROBE_WAIT = env.int("IQUERY_PROBE_WAIT", default=300)
IQUERY_COURT_BLOCKED_WAIT = env.int("IQUERY_COURT_BLOCKED_WAIT", default=600)
IQUERY_COURT_TIMEOUT_WAIT = env.int("IQUERY_COURT_TIMEOUT_WAIT", default=10)
# IQUERY_SWEEP_BATCH_SIZE should be lower than the celery visibility_timeout
IQUERY_SWEEP_BATCH_SIZE = env.int("IQUERY_SWEEP_BATCH_SIZE", default=10_800)
