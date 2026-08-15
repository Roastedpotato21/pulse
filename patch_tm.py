import re


def patch_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Add TaskConcurrencyError
    if "class TaskConcurrencyError" not in content:
        content = content.replace("class TaskStatus(Enum):", 
"""class TaskConcurrencyError(Exception):
    \"\"\"Raised when a stale task update is rejected via OCC.\"\"\"
    def __init__(self, task_id: str):
        super().__init__(f"Task {task_id} was modified by another process. Stale update rejected.")
        self.task_id = task_id


class TaskStatus(Enum):""")

    # 2. Add version to Task
    if "version: int = 1" not in content:
        content = content.replace("    metadata: dict[str, Any] = field(default_factory=dict)\n",
"""    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = 1\n""")

    # 3. Add version to to_dict
    content = content.replace('"metadata": self.metadata,\n        }', 
                              '"metadata": self.metadata,\n            "version": self.version,\n        }')
                              
    # 4. Add version to from_dict
    content = content.replace('metadata=data.get("metadata", {}),\n        )',
                              'metadata=data.get("metadata", {}),\n            version=int(data.get("version", 1)),\n        )')

    # 5. Schema changes
    if "version INTEGER NOT NULL" not in content:
        content = content.replace("metadata TEXT NOT NULL,\n                    checkpoints TEXT NOT NULL,\n                    history TEXT NOT NULL\n                );",
"""metadata TEXT NOT NULL,
                    checkpoints TEXT NOT NULL,
                    history TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                );""")
        
    # Add migration script
    if "ALTER TABLE tasks ADD COLUMN version" not in content:
        content = content.replace('CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);\n                """\n            )',
"""CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                \"\"\"
            )
            # Safe schema migration for OCC
            cursor = conn.execute("PRAGMA table_info(tasks)")
            columns = [row[1] for row in cursor.fetchall()]
            if "version" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            """)

    # 6. Change TaskStore._insert_task to create_task and update_task
    # First remove old _insert_task and save_task
    content = re.sub(r'    def _insert_task\(self.*?def load\(self', '    def load(self', content, flags=re.DOTALL)
    
    # Now insert create_task and update_task before load
    new_methods = """    def create_task(self, task: Task) -> None:
        \"\"\"Insert a newly created task.\"\"\"
        try:
            with self._connect() as conn:
                conn.execute(
                    \"\"\"
                    INSERT INTO tasks (
                        id, title, goal, priority, status, progress, retries, 
                        max_retries, depends_on, created_at, updated_at, 
                        result, error, metadata, checkpoints, history, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    \"\"\",
                    (
                        task.id, task.title, task.goal, task.priority.name,
                        task.status.value, task.progress, task.retries,
                        task.max_retries, json.dumps(task.depends_on),
                        task.created_at, task.updated_at, task.result,
                        task.error, json.dumps(task.metadata),
                        json.dumps([asdict(cp) for cp in task.checkpoints]),
                        json.dumps([asdict(rec) for rec in task.history]),
                        task.version,
                    )
                )
        except sqlite3.Error as err:
            logger.error(f"Failed to create task {task.id}: {err}")
            raise

    def update_task(self, task: Task) -> None:
        \"\"\"Update an existing task safely using OCC.\"\"\"
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    \"\"\"
                    UPDATE tasks SET
                        title=?, goal=?, priority=?, status=?, progress=?,
                        retries=?, max_retries=?, depends_on=?, created_at=?,
                        updated_at=?, result=?, error=?, metadata=?,
                        checkpoints=?, history=?, version=version + 1
                    WHERE id = ? AND version = ?
                    \"\"\",
                    (
                        task.title, task.goal, task.priority.name,
                        task.status.value, task.progress, task.retries,
                        task.max_retries, json.dumps(task.depends_on),
                        task.created_at, task.updated_at, task.result,
                        task.error, json.dumps(task.metadata),
                        json.dumps([asdict(cp) for cp in task.checkpoints]),
                        json.dumps([asdict(rec) for rec in task.history]),
                        task.id, task.version,
                    )
                )
                if cursor.rowcount == 0:
                    raise TaskConcurrencyError(task.id)
        except sqlite3.Error as err:
            logger.error(f"Failed to update task {task.id}: {err}")
            raise

"""
    content = content.replace("    def load(self", new_methods + "    def load(self")

    # In _migrate_legacy_json, change _insert_task to create_task
    content = content.replace("self._insert_task(conn, task)", "self.create_task(task)")

    # 7. Add version to load()
    content = content.replace('"history": json.loads(row_dict["history"]),\n                    }',
                              '"history": json.loads(row_dict["history"]),\n                        "version": row_dict["version"],\n                    }')

    # 8. Update TaskManager methods.
    # Every method that mutates needs to handle TaskConcurrencyError.
    def add_occ_handling(method_str):
        # Finds self.store.save_task(task) and replaces it with OCC logic
        return method_str.replace("self.store.save_task(task)", 
"""try:
                self.store.update_task(task)
                task.version += 1
            except TaskConcurrencyError:
                # Reload the authoritative state from DB to heal our cache
                fresh_tasks = self.store.load()
                if task.id in fresh_tasks:
                    self._tasks[task.id] = fresh_tasks[task.id]
                raise""")

    # We need to replace all self.store.save_task(task) inside async with self._lock: blocks.
    # But wait, create_task should use self.store.create_task(task) instead!
    
    # First, handle create_task
    content = content.replace(
        "            self._tasks[task_id] = task\n            self.store.save_task(task)",
        "            self._tasks[task_id] = task\n            self.store.create_task(task)"
    )

    # Now replace the rest with OCC handling
    content = add_occ_handling(content)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

if __name__ == "__main__":
    patch_file("src/pulse/task_manager.py")
