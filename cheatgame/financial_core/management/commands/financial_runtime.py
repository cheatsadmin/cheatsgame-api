import json

from django.core.management.base import BaseCommand, CommandError

from cheatgame.financial_core.services.runtime import (
    RUNTIME_STAGES,
    due_runtime_work_ids,
    execute_runtime_work,
    get_runtime_policy,
    make_runtime_work_due,
    run_runtime_batch,
    runtime_stats,
)


class Command(BaseCommand):
    help = "Inspect or explicitly execute one bounded Digital financial-runtime batch."

    ACTIONS = ("inspect", "run-one", "run-batch", "retry", "stats")

    def add_arguments(self, parser):
        parser.add_argument("action", choices=self.ACTIONS)
        parser.add_argument(
            "--stage",
            choices=(*RUNTIME_STAGES, "all"),
            default="all",
        )
        parser.add_argument("--work-id", type=int)
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--apply", action="store_true")

    def _validate_work_target(self, *, action, stage, work_id):
        if action in ("run-one", "retry"):
            if stage not in RUNTIME_STAGES or work_id is None:
                raise CommandError(
                    f"{action} requires one explicit --stage and --work-id."
                )

    def handle(self, *args, **options):
        action = options["action"]
        stage = options["stage"]
        work_id = options["work_id"]
        apply = bool(options["apply"])
        policy = get_runtime_policy()
        limit = min(max(1, int(options["limit"])), policy.max_batch_size)
        self._validate_work_target(
            action=action,
            stage=stage,
            work_id=work_id,
        )

        if action == "stats":
            self.stdout.write(json.dumps(runtime_stats(), sort_keys=True))
            return

        if action == "inspect":
            stages = RUNTIME_STAGES if stage == "all" else (stage,)
            payload = {
                item: due_runtime_work_ids(stage=item, limit=limit)
                for item in stages
            }
            self.stdout.write(json.dumps(payload, sort_keys=True))
            return

        if action == "run-batch":
            if not apply:
                payload = {
                    item: due_runtime_work_ids(stage=item, limit=limit)
                    for item in RUNTIME_STAGES
                }
                self.stdout.write(
                    f"dry_run=true {json.dumps(payload, sort_keys=True)}"
                )
                return
            result = run_runtime_batch(limit=limit)
            self.stdout.write(
                json.dumps(
                    [item.__dict__ for item in result.results],
                    sort_keys=True,
                )
            )
            return

        if not apply:
            self.stdout.write(
                f"dry_run=true stage={stage} work_id={work_id}"
            )
            return
        if action == "retry":
            make_runtime_work_due(stage=stage, work_id=work_id)
        result = execute_runtime_work(stage=stage, work_id=work_id)
        self.stdout.write(json.dumps(result.__dict__, sort_keys=True))
