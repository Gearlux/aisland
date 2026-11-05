"""CI and the test runs: `ci_scaffold` works out a project's facts and `ci_templates` renders its CI files from a
template set (`aisland jenkins scaffold`), `quality` runs the gates locally (`aisland check`), `stats_report` reads the
test-run statistics (`aisland tests report`) and `nightly` the orchestrator's nights (`aisland tests nightly`). The
pytest plugin that records the runs stays `aisland.pytest_stats`, its entry point. Nothing is imported here.
"""
