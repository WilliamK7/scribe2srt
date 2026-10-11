"""A process lock protects Qt local IPC; senders exit only after an acknowledgement."""

import hashlib
import json
import os
import time
import uuid

from PySide6.QtCore import QObject, QLockFile, QStandardPaths, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .app_paths import application_directory
from .launch import path_key


MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class InstanceError(RuntimeError):
    pass


def instance_name(directory=None, user=None) -> str:
    directory = path_key(str(directory or application_directory()))
    user = user or os.path.normcase(os.path.expanduser("~"))
    digest = hashlib.sha256((user + "\0" + directory).encode("utf-8")).hexdigest()[:32]
    return "scribe2srt-" + digest


class SingleInstance(QObject):
    def __init__(self, directory=None, parent=None, timeout_ms=10000):
        super().__init__(parent)
        self.name = instance_name(directory)
        temporary = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)
        self.lock = QLockFile(os.path.join(temporary, self.name + ".lock"))
        self.lock.setStaleLockTime(0)  # A long transcription must not make a live lock stale.
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.setMaxPendingConnections(64)
        self.server.newConnection.connect(self._accept_connections)
        self.timeout_ms = timeout_ms
        self.handler = None
        self.owner = False
        self._clients = {}

    def acquire_or_forward(self, send_to: bool, files) -> bool:
        """Return True for the owner, False after successful delivery to the owner."""
        deadline = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.lock.tryLock(0):
                self.owner = True
                # Only the lock owner may remove a crashed Unix server's endpoint.
                QLocalServer.removeServer(self.name)
                if not self.server.listen(self.name):
                    reason = self.server.errorString()
                    self.close()
                    raise InstanceError(f"无法启动本地任务接收服务：{reason}")
                return True
            if self.lock.error() != QLockFile.LockError.LockFailedError:
                raise InstanceError(f"无法创建进程锁（{self.lock.error().name}），请检查临时目录权限。")
            socket = QLocalSocket()
            socket.connectToServer(self.name)
            if socket.waitForConnected(min(200, self._remaining_ms(deadline))):
                try:
                    self._forward(socket, send_to, files, deadline)
                finally:
                    socket.abort()
                return False
            socket.abort()
            time.sleep(0.03)
        raise InstanceError("另一实例正在启动或无法连接；文件尚未确认接收，请稍后重新发送。")

    @staticmethod
    def _remaining_ms(deadline):
        return max(1, int((deadline - time.monotonic()) * 1000))

    def _forward(self, socket, send_to, files, deadline):
        request_id = uuid.uuid4().hex
        payload = json.dumps({"version": 1, "request_id": request_id,
                              "send_to": send_to, "files": list(files)}, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(payload) > MAX_MESSAGE_BYTES:
            raise InstanceError("发送的文件列表过长，请分批发送。")
        if socket.write(payload) != len(payload):
            raise InstanceError("任务发送失败，请重新发送。")
        if socket.bytesToWrite() and not socket.waitForBytesWritten(self._remaining_ms(deadline)):
            raise InstanceError("未能发送完整任务，请重新发送。")
        response = bytearray()
        while time.monotonic() < deadline:
            response.extend(bytes(socket.readAll()))
            if len(response) > MAX_MESSAGE_BYTES:
                break
            if b"\n" in response:
                try:
                    ack = json.loads(response.split(b"\n", 1)[0].decode("utf-8"))
                    if not isinstance(ack, dict) or ack.get("request_id") != request_id:
                        break
                    if ack.get("ok") is not True:
                        raise InstanceError(ack.get("error") or "主窗口拒绝了任务，请稍后重新发送。")
                    return
                except (ValueError, UnicodeError):
                    break
            if not socket.waitForReadyRead(self._remaining_ms(deadline)):
                # A disconnect can leave the final acknowledgement in the read buffer.
                if socket.bytesAvailable():
                    continue
                break
        raise InstanceError("未收到有效的接收确认，任务可能已送达；请检查主窗口后重新发送。")

    def _accept_connections(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.timeout.connect(socket.abort)
            self._clients[socket] = {"buffer": bytearray(), "timer": timer, "handled": False}
            socket.readyRead.connect(lambda client=socket: self._read_request(client))
            socket.disconnected.connect(lambda client=socket: self._forget_client(client))
            timer.start(self.timeout_ms)
            self._read_request(socket)

    def _read_request(self, socket):
        state = self._clients.get(socket)
        if not state or state["handled"]:
            return
        state["buffer"].extend(bytes(socket.readAll()))
        if len(state["buffer"]) > MAX_MESSAGE_BYTES:
            self._respond(socket, {"ok": False, "error": "文件列表过长。"})
            return
        if b"\n" not in state["buffer"]:
            return
        request_id = None
        try:
            request = json.loads(state["buffer"].split(b"\n", 1)[0].decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("无效的任务格式。")
            request_id = request.get("request_id")
            files = request.get("files")
            send_to = request.get("send_to")
            if (request.get("version") != 1 or not isinstance(request_id, str)
                    or not isinstance(send_to, bool) or not isinstance(files, list)
                    or any(not isinstance(path, str) or not os.path.isabs(path) for path in files)
                    or (send_to and not files) or (not send_to and files)):
                raise ValueError("无效的任务参数，请重新启动程序。")
            if self.handler is None:
                raise ValueError("主窗口尚未就绪，请稍后重新发送。")
            result = dict(self.handler(send_to, files))
        except Exception as exc:
            result = {"ok": False, "error": f"无法接收任务：{exc}"}
        result["request_id"] = request_id
        self._respond(socket, result)

    def _respond(self, socket, response):
        state = self._clients.get(socket)
        if state is None:
            return
        state["handled"] = True
        socket.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
        socket.flush()
        socket.disconnectFromServer()

    def _forget_client(self, socket):
        self._clients.pop(socket, None)
        socket.deleteLater()

    def prepare_shutdown(self) -> bool:
        """Keep the event loop alive until every connected sender has been answered."""
        self._accept_connections()
        for socket in list(self._clients):
            self._read_request(socket)
        return not self._clients and not self.server.hasPendingConnections()

    def close(self):
        self.server.close()
        for socket in list(self._clients):
            socket.abort()
        if self.owner:
            self.lock.unlock()
            self.owner = False
