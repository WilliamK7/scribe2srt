"""Offline regression coverage for Explorer entry, queue lifetime and Windows IPC."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QLockFile, Signal, Slot
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from core.app_paths import application_directory
from core.config import SETTINGS_FILE
from core.launch import LaunchError, parse_launch_arguments, path_key
from core.single_instance import InstanceError, SingleInstance, instance_name
from core import windows_integration as integration
from ui.main_window import MainWindow


APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent.parent


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


class ControlledWorker(QObject):
    finished = Signal(str)
    error = Signal(str)
    log_message = Signal(str)
    progress_updated = Signal(int, int)
    chunk_progress = Signal(int, str, str)
    chunks_ready = Signal(list)
    subtitle_progress = Signal(int, int)
    created = []

    def __init__(self, **kwargs):
        super().__init__()
        self.options = kwargs
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = False
        self.failure = None
        self.total_chunks = 1
        self.current_chunk_index = 0
        self.created.append(self)

    @Slot()
    def run(self):
        self.started.set()
        if not self.release.wait(8):
            self.error.emit("mock worker timed out")
        elif self.cancelled:
            self.error.emit("任务被用户取消。")
        elif self.failure:
            self.error.emit(self.failure)
        else:
            self.finished.emit("任务成功完成！")

    def request_cancellation(self):
        self.cancelled = True

    def get_state(self):
        return {"file_path": self.options["file_path"], "original_file_path": self.options["original_file_path"]}


class LaunchTests(unittest.TestCase):
    def test_manual_launch_and_send_to_with_chinese_spaces_and_option_like_names(self):
        self.assertEqual(parse_launch_arguments([]).files, [])
        self.assertFalse(parse_launch_arguments([]).send_to)
        args = parse_launch_arguments(["--send-to", "--", "中文 带空格.json", "--example.json"])
        self.assertTrue(args.send_to)
        self.assertEqual(args.files, [os.path.abspath("中文 带空格.json"), os.path.abspath("--example.json")])

    def test_missing_flag_or_files_and_unknown_options_fail_explicitly(self):
        for arguments in (["--send-to"], ["--send-to", "--"], ["input.json"], ["--unknown"]):
            with self.subTest(arguments=arguments), self.assertRaises(LaunchError):
                parse_launch_arguments(arguments)

    def test_settings_are_absolute_and_frozen_path_uses_exe_directory(self):
        self.assertEqual(Path(SETTINGS_FILE), ROOT / "settings.json")
        with tempfile.TemporaryDirectory() as temporary, patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(Path(temporary, "程序 空格", "Scribe2SRT.exe"))):
            self.assertEqual(application_directory(), Path(temporary, "程序 空格").resolve())

    def test_windows_path_identity_ignores_case_and_normalizes_separators(self):
        with patch("core.launch.sys.platform", "win32"):
            self.assertEqual(path_key(str(ROOT / "中文 文件.JSON")), path_key(str(ROOT / "中文 文件.json").upper()))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="scribe2srt-队列 ")
        self.settings_path = Path(self.directory.name, "settings.json")
        self.patches = [patch("ui.main_window.SETTINGS_FILE", str(self.settings_path)),
                        patch("ui.main_window.is_ffmpeg_available", return_value=False),
                        patch("ui.main_window.Worker", ControlledWorker),
                        patch("ui.main_window.QMessageBox.information"),
                        patch("ui.main_window.QMessageBox.exec", return_value=0)]
        self.addCleanup(self.directory.cleanup)
        for item in self.patches:
            value = item.start()
            if item is self.patches[-2]:
                self.information = value
            if item is self.patches[-1]:
                self.dialog_exec = value
            self.addCleanup(item.stop)
        ControlledWorker.created = []
        self.window = MainWindow()
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        self.window._auto_exit_timer.stop()
        if self.window._queue_active:
            self.window.cancel_process()
        for worker in ControlledWorker.created:
            worker.release.set()
        wait_until(lambda: self.window.thread is None)
        self.window.close()

    def source(self, name="中文 空格.json"):
        path = Path(self.directory.name, name)
        path.write_text("{}", encoding="utf-8")
        return str(path)

    def worker(self, number=1):
        self.assertTrue(wait_until(lambda: len(ControlledWorker.created) >= number
                                   and ControlledWorker.created[number - 1].started.is_set()))
        return ControlledWorker.created[number - 1]

    def complete(self, number=1, error=None):
        worker = self.worker(number)
        worker.failure = error
        worker.release.set()
        return worker

    def test_first_external_file_starts_after_ui_initialization_without_dialogs(self):
        source = self.source()
        result = self.window.enqueue_external_files([source])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(ControlledWorker.created, [])
        self.assertFalse(self.window.select_button.isEnabled())
        self.assertEqual(self.worker().options["file_path"], source)
        self.complete()
        self.assertTrue(wait_until(lambda: not self.window._queue_active))
        self.assertIsNone(self.window.thread)
        self.assertIn(str(Path(source).with_suffix(".srt")), self.window.log_area.toPlainText())
        self.information.assert_not_called()
        self.dialog_exec.assert_not_called()

    def test_busy_single_manual_file_converts_to_queue_without_overwriting_state(self):
        first, second = self.source("first.json"), self.source("second.json")
        self.window.set_files([first])
        self.window.start_process()
        original_worker = self.worker()
        retry_state = {"preserve": True}
        self.window._pending_retry_state = retry_state
        result = self.window.enqueue_external_files([second, first, str(Path(second).parent / "." / Path(second).name)])
        self.assertEqual((result["accepted"], result["duplicates"]), (1, 2))
        self.assertIs(self.window.worker, original_worker)
        self.assertIs(self.window._pending_retry_state, retry_state)
        self.assertEqual(self.window.current_processing_file, first)
        self.assertEqual(self.window.batch_queue, [first, second])
        self.complete()
        self.complete(2)
        self.assertTrue(wait_until(lambda: not self.window._queue_active))
        self.assertEqual([item["file"] for item in self.window.batch_results], [first, second])
        self.information.assert_not_called()

    def test_appended_files_share_original_queue_options_and_translation_limiter(self):
        first, second = self.source("one.json"), self.source("two.json")
        self.window.lang_combo.setCurrentText("日语")
        self.window.audio_events_checkbox.setChecked(True)
        self.window.enqueue_external_files([first])
        original = self.worker()
        self.window.settings.update({"translation_model": "changed", "max_concurrent_chunks": 9})
        self.window.lang_combo.setCurrentText("英文")
        self.window.audio_events_checkbox.setChecked(False)
        self.window.enqueue_external_files([second])
        self.complete()
        appended = self.worker(2)
        for key in ("language_code", "tag_audio_events", "subtitle_settings", "translation_settings", "max_concurrent_chunks"):
            self.assertEqual(appended.options[key], original.options[key])
        self.assertIs(appended.options["translation_rate_limiter"], original.options["translation_rate_limiter"])
        self.complete(2)
        self.assertTrue(wait_until(lambda: not self.window._queue_active))

    def test_append_during_manual_retry_wait_preserves_and_executes_retry_before_new_file(self):
        first, second = self.source("retry.json"), self.source("appended.json")
        def choose_retry():
            box = self.window.findChildren(QMessageBox)[-1]
            next(button for button in box.buttons() if button.text() == "重试").click()
            return 0
        self.dialog_exec.side_effect = choose_retry
        self.window.set_files([first])
        self.window.start_process()
        self.complete(error="retry this file")
        self.assertTrue(wait_until(lambda: self.window.thread is None and self.window._pending_retry_state is not None))
        state = self.window._pending_retry_state
        self.window.enqueue_external_files([second])
        self.assertIs(self.window._pending_retry_state, state)
        retry = self.worker(2)
        self.assertIs(retry.options["restore_state"], state)
        self.assertEqual(retry.options["original_file_path"], first)
        self.complete(2)
        self.assertEqual(self.worker(3).options["original_file_path"], second)
        self.complete(3)
        self.assertTrue(wait_until(lambda: not self.window._queue_active))
        self.assertEqual([item["file"] for item in self.window.batch_results], [first, second])
        self.dialog_exec.assert_called_once()
        self.information.assert_not_called()

    def test_invalid_paths_are_reported_and_valid_file_still_runs(self):
        source = self.source()
        missing = str(Path(self.directory.name, "不存在.json"))
        result = self.window.enqueue_external_files([self.directory.name, missing, source])
        self.assertEqual((result["accepted"], result["errors"]), (1, 2))
        self.complete()
        self.assertTrue(wait_until(lambda: not self.window._queue_active))
        self.assertEqual([item["status"] for item in self.window.batch_results], ["error", "error", "success"])
        self.assertTrue(self.window._keep_open)
        self.assertIn("文件夹不支持", self.window.log_area.toPlainText())
        self.assertIn("失败 2", self.window.log_area.toPlainText())

    def test_only_invalid_inputs_finish_and_keep_send_to_window(self):
        self.window.started_from_send_to = True
        self.window.enqueue_external_files([str(Path(self.directory.name, "missing.json"))])
        self.assertTrue(wait_until(lambda: not self.window._queue_active))
        self.assertFalse(ControlledWorker.created)
        self.assertFalse(self.window._can_auto_exit())

    def test_file_removed_while_waiting_fails_and_later_file_continues(self):
        first, missing, last = [self.source(name) for name in ("one.json", "missing.json", "last.json")]
        self.window.enqueue_external_files([first, missing, last])
        self.worker()
        Path(missing).unlink()
        self.complete()
        self.assertEqual(self.worker(2).options["file_path"], last)
        self.complete(2)
        self.assertTrue(wait_until(lambda: not self.window._queue_active))
        self.assertEqual([item["status"] for item in self.window.batch_results], ["success", "error", "success"])

    def test_failure_continues_without_retry_and_preserves_auto_started_window(self):
        first, second = self.source("bad.json"), self.source("good.json")
        self.window.started_from_send_to = True
        self.window.enqueue_external_files([first, second])
        self.complete(error="expected failure")
        self.complete(2)
        self.assertTrue(wait_until(lambda: not self.window._queue_active))
        self.assertEqual([item["status"] for item in self.window.batch_results], ["error", "success"])
        self.dialog_exec.assert_not_called()
        with patch.object(self.window, "close") as close:
            self.window._try_auto_exit()
            close.assert_not_called()

    def test_cancellation_stops_current_and_pending_and_rejects_arrivals_during_cleanup(self):
        first, second, later = [self.source(name) for name in ("cancel.json", "skip.json", "later.json")]
        self.window.started_from_send_to = True
        self.window.enqueue_external_files([first, second])
        self.worker()
        self.window.cancel_process()
        result = self.window.enqueue_external_files([later])
        self.assertFalse(result["ok"])
        self.assertIn("稍后重新发送", result["error"])
        self.assertEqual(self.window.batch_queue, [first, second])
        self.complete()
        self.assertTrue(wait_until(lambda: not self.window._queue_active))
        self.assertEqual(len(ControlledWorker.created), 1)
        self.assertFalse(self.window._can_auto_exit())
        self.assertIn("未处理 1", self.window.log_area.toPlainText())
        self.assertTrue(self.window.enqueue_external_files([later])["ok"])
        self.complete(2)
        self.assertTrue(wait_until(lambda: not self.window._queue_active))
        self.assertFalse(self.window._can_auto_exit())

    def test_successful_send_to_window_closes_only_after_thread_has_ended(self):
        self.window.started_from_send_to = True
        self.window.enqueue_external_files([self.source()])
        with patch.object(self.window, "close") as close:
            self.window._try_auto_exit()
            close.assert_not_called()
            self.complete()
            self.assertTrue(wait_until(lambda: close.called))
            self.assertIsNone(self.window.thread)
            self.assertFalse(self.window._queue_active)
            close.assert_called_once()

    def test_manually_opened_window_remains_after_external_success(self):
        self.window.enqueue_external_files([self.source()])
        self.complete()
        self.assertTrue(wait_until(lambda: not self.window._queue_active))
        with patch.object(self.window, "close") as close:
            self.window._try_auto_exit()
            close.assert_not_called()

    def test_completed_file_can_be_resent_before_auto_exit(self):
        source = self.source()
        self.window.started_from_send_to = True
        with patch.object(self.window, "close") as close:
            self.window.enqueue_external_files([source])
            self.complete()
            self.assertTrue(wait_until(lambda: not self.window._queue_active))
            self.assertTrue(self.window._auto_exit_timer.isActive())
            self.assertEqual(self.window.enqueue_external_files([source])["accepted"], 1)
            self.assertFalse(self.window._auto_exit_timer.isActive())
            self.worker(2)
            close.assert_not_called()
            self.complete(2)
            self.assertTrue(wait_until(lambda: close.called))

    def test_arrival_during_thread_cleanup_is_processed_before_closing(self):
        first, second = self.source("first.json"), self.source("cleanup.json")
        self.window.started_from_send_to = True
        completion = self.window._handle_task_completion
        received = []
        def cleanup():
            if not received:
                received.append(self.window.enqueue_external_files([second]))
            completion()
        with patch.object(self.window, "_handle_task_completion", side_effect=cleanup), patch.object(self.window, "close") as close:
            self.window.enqueue_external_files([first])
            self.complete()
            self.worker(2)
            close.assert_not_called()
            self.complete(2)
            self.assertTrue(wait_until(lambda: close.called))
        self.assertEqual(received[0]["accepted"], 1)

    def test_arrival_during_shutdown_drain_cancels_exit_and_runs_new_task(self):
        first, second = self.source("one.json"), self.source("shutdown.json")
        self.window.started_from_send_to = True
        drained = []
        def drain():
            if not drained:
                drained.append(True)
                self.window.enqueue_external_files([second])
            return True
        self.window._before_auto_close = drain
        with patch.object(self.window, "close") as close:
            self.window.enqueue_external_files([first])
            self.complete()
            self.worker(2)
            close.assert_not_called()
            self.complete(2)
            self.assertTrue(wait_until(lambda: close.called))

    def test_source_options_persist_and_settings_load_from_another_working_directory(self):
        self.window.lang_combo.setCurrentText("韩语")
        self.window.audio_events_checkbox.setChecked(True)
        previous = os.getcwd()
        try:
            os.chdir(self.directory.name)
            other = MainWindow()
            self.addCleanup(other.close)
            self.assertEqual(other.lang_combo.currentText(), "韩语")
            self.assertTrue(other.audio_events_checkbox.isChecked())
        finally:
            os.chdir(previous)
        settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual((settings["source_language"], settings["tag_audio_events"]), ("ko", True))

    def test_settings_read_and_write_failures_are_visible_and_prevent_auto_exit(self):
        self.settings_path.write_text("[]", encoding="utf-8")
        with patch.object(self.window, "_show_notice") as notice:
            self.window.load_settings()
            self.assertIn(str(self.settings_path), self.window.log_area.toPlainText())
            self.assertIn("无法读取", self.window.log_area.toPlainText())
            with patch("builtins.open", side_effect=PermissionError("cannot read configuration")):
                self.window.load_settings()
            self.assertIn("cannot read configuration", self.window.log_area.toPlainText())
            with patch("ui.main_window.atomic_write_text", side_effect=PermissionError("read only")):
                self.assertFalse(self.window.save_settings())
            self.assertIn("本次修改尚未保存", self.window.log_area.toPlainText())
            self.assertEqual(notice.call_count, 3)
        self.assertTrue(self.window._keep_open)


class IPCTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="scribe2srt-ipc ")
        self.addCleanup(self.directory.cleanup)
        self.instance = SingleInstance(self.directory.name, timeout_ms=1000)
        self.addCleanup(self.instance.close)
        self.processes = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)

    def child(self, files, *arguments):
        command = [sys.executable, "-m", "tests.send_to_ipc_helper", "--directory", self.directory.name,
                   "--files", json.dumps(files, ensure_ascii=False), *arguments]
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace",
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        self.processes.append(process)
        return process

    def result(self, process):
        self.assertTrue(wait_until(lambda: process.poll() is not None, timeout=10))
        output, errors = process.communicate(timeout=1)
        self.assertTrue(output.strip(), errors)
        return json.loads(output.strip())

    def test_instance_identity_varies_by_user_and_application_directory(self):
        self.assertNotEqual(instance_name(self.directory.name, "alice"), instance_name(self.directory.name, "bob"))
        self.assertNotEqual(instance_name(self.directory.name, "alice"), instance_name(str(ROOT), "alice"))

    def test_simultaneous_processes_have_one_owner_and_deliver_complete_file_lists(self):
        barrier = str(Path(self.directory.name, "start"))
        receipts = str(Path(self.directory.name, "received.json"))
        lists = [[str(Path(self.directory.name, f"中文 空格 {number}.json")),
                  str(Path(self.directory.name, f"--特殊 '{number}.mp4"))] for number in range(4)]
        processes = [self.child(files, "--mode", "race", "--barrier", barrier, "--receipts", receipts,
                                "--expected", "4") for files in lists]
        Path(barrier).touch()
        results = [self.result(process) for process in processes]
        self.assertEqual(sum(result["status"] == "owner" for result in results), 1, results)
        self.assertEqual(sum(result["status"] == "forwarded" for result in results), 3, results)
        received = json.loads(Path(receipts).read_text(encoding="utf-8"))
        self.assertCountEqual([item["files"] for item in received], lists)
        self.assertTrue(all(item["send_to"] for item in received))

    def test_forwarding_waits_for_acceptance_and_preserves_unicode_paths(self):
        self.assertTrue(self.instance.acquire_or_forward(False, []))
        received = []
        self.instance.handler = lambda send_to, files: (received.append(files) or {"ok": True})
        files = [str(Path(self.directory.name, "中文 文件.json")), str(Path(self.directory.name, "second file.mp3"))]
        process = self.child(files)
        self.assertEqual(self.result(process)["status"], "forwarded")
        self.assertEqual(received, [files])

    def test_cancellation_rejection_is_reported_to_the_sender(self):
        self.assertTrue(self.instance.acquire_or_forward(False, []))
        self.instance.handler = lambda *_: {"ok": False, "error": "任务正在取消，请稍后重新发送文件。"}
        result = self.result(self.child([str(Path(self.directory.name, "later.json"))]))
        self.assertEqual(result["status"], "error")
        self.assertIn("稍后重新发送", result["error"])

    def test_lock_without_listener_reports_startup_or_communication_failure(self):
        self.assertTrue(self.instance.lock.tryLock(0))
        self.addCleanup(self.instance.lock.unlock)
        result = self.result(self.child([str(Path(self.directory.name, "file.json"))], "--timeout-ms", "250"))
        self.assertEqual(result["status"], "error")
        self.assertIn("尚未确认接收", result["error"])

    def test_lost_acknowledgement_is_not_silently_treated_as_success(self):
        self.assertTrue(self.instance.acquire_or_forward(False, []))
        received = []
        self.instance.handler = lambda *_: (received.append(True) or {"ok": True})
        with patch.object(self.instance, "_respond"):
            result = self.result(self.child([str(Path(self.directory.name, "file.json"))], "--timeout-ms", "300"))
        self.assertEqual(result["status"], "error")
        self.assertIn("任务可能已送达", result["error"])
        self.assertEqual(received, [True])

    def test_shutdown_waits_for_fragmented_request_and_acknowledgement(self):
        self.assertTrue(self.instance.acquire_or_forward(False, []))
        received = []
        self.instance.handler = lambda _, files: (received.append(files) or {"ok": True})
        socket = QLocalSocket()
        self.addCleanup(socket.abort)
        socket.connectToServer(self.instance.name)
        self.assertTrue(socket.waitForConnected(1000))
        APP.processEvents()
        files = [str(Path(self.directory.name, "退出时 新任务.json"))]
        message = json.dumps({"version": 1, "request_id": "shutdown-test", "send_to": True,
                              "files": files}, ensure_ascii=False).encode("utf-8") + b"\n"
        socket.write(message[:11])
        socket.flush()
        APP.processEvents()
        self.assertFalse(self.instance.prepare_shutdown())
        socket.write(message[11:])
        socket.flush()
        self.assertTrue(wait_until(lambda: bool(received)))
        self.assertEqual(received, [files])
        self.assertTrue(wait_until(lambda: self.instance.prepare_shutdown()))
        response = json.loads(bytes(socket.readAll()).decode("utf-8"))
        self.assertTrue(response["ok"])
        self.assertEqual(response["request_id"], "shutdown-test")


@unittest.skipUnless(sys.platform == "win32", "Windows shortcut integration")
class ShortcutTests(unittest.TestCase):
    def test_source_shortcut_targets_current_pythonw_and_absolute_entry(self):
        spec = integration.shortcut_spec()
        self.assertEqual(spec.target, str(Path(sys.executable).resolve().with_name("pythonw.exe")))
        self.assertEqual(spec.arguments, subprocess.list2cmdline([str(ROOT / "app.py"), "--send-to", "--"]))
        self.assertEqual(spec.working_directory, str(ROOT))

    def test_frozen_shortcut_targets_exe_and_not_bundle_temp_directory(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(Path(temporary, "程序 空格.exe"))):
            spec = integration.shortcut_spec()
        self.assertEqual(spec.target, str(Path(temporary, "程序 空格.exe")))
        self.assertEqual(spec.arguments, "--send-to --")
        self.assertEqual(spec.working_directory, temporary)

    def test_real_com_shortcut_add_update_remove_preserve_other_files_and_quote_paths(self):
        with tempfile.TemporaryDirectory(prefix="scribe2srt-中文 空格 '$` ") as temporary:
            other = Path(temporary, "Other.lnk")
            other.write_bytes(b"preserve other shortcuts")
            spec = integration.ShortcutSpec(str(Path(sys.executable).with_name("pythonw.exe")),
                                            subprocess.list2cmdline([str(ROOT / "app.py"), "--send-to", "--"]), str(ROOT))
            with patch.object(integration, "shortcut_spec", return_value=spec):
                path = integration.add_send_to_shortcut(temporary)
                self.assertEqual(Path(path), Path(temporary, integration.SHORTCUT_NAME))
                path_again = integration.add_send_to_shortcut(temporary)
                self.assertEqual(path_again, path)
            fields = integration._run_powershell(
                "$shell = New-Object -ComObject WScript.Shell\n"
                f"$shortcut = $shell.CreateShortcut({integration._ps_string(path)})\n"
                "[PSCustomObject]@{target=$shortcut.TargetPath; arguments=$shortcut.Arguments; directory=$shortcut.WorkingDirectory} | ConvertTo-Json -Compress\n")
            fields = json.loads(fields)
            self.assertEqual(os.path.normcase(fields["target"]), os.path.normcase(spec.target))
            self.assertEqual(fields["arguments"], spec.arguments)
            self.assertEqual(os.path.normcase(fields["directory"]), os.path.normcase(spec.working_directory))
            integration.remove_send_to_shortcut(temporary)
            integration.remove_send_to_shortcut(temporary)
            self.assertFalse(Path(path).exists())
            self.assertEqual(other.read_bytes(), b"preserve other shortcuts")

    def test_same_named_foreign_shortcut_is_not_overwritten_or_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, integration.SHORTCUT_NAME)
            integration._run_powershell(
                "$shell = New-Object -ComObject WScript.Shell\n"
                f"$shortcut = $shell.CreateShortcut({integration._ps_string(path)})\n"
                "$shortcut.TargetPath = 'notepad.exe'\n$shortcut.Description = 'Another program'\n$shortcut.Save()\n")
            with self.assertRaises(integration.IntegrationError):
                integration.add_send_to_shortcut(temporary)
            with self.assertRaises(integration.IntegrationError):
                integration.remove_send_to_shortcut(temporary)
            self.assertTrue(path.exists())
