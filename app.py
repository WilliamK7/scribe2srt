import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from ui.main_window import MainWindow
from core.config import STYLESHEET
from core.launch import LaunchError, parse_launch_arguments
from core.single_instance import InstanceError, SingleInstance


def main(argv=None):
    app = QApplication([sys.argv[0]])
    app.setApplicationName("Scribe2SRT")
    app.setStyleSheet(STYLESHEET)
    instance = None
    try:
        launch = parse_launch_arguments(argv)
        if sys.platform == "win32":
            instance = SingleInstance(parent=app)
            if not instance.acquire_or_forward(launch.send_to, launch.files):
                return 0
            app.aboutToQuit.connect(instance.close)
        window = MainWindow(started_from_send_to=launch.send_to)
        if instance is not None:
            instance.handler = window.handle_launch_request
            window._before_auto_close = instance.prepare_shutdown
        window.show()
        if launch.send_to:
            QTimer.singleShot(0, lambda: window.enqueue_external_files(launch.files))
        return app.exec()
    except (LaunchError, InstanceError) as exc:
        QMessageBox.critical(None, "Scribe2SRT 启动失败", str(exc))
        return 1
    finally:
        if instance is not None:
            instance.close()


if __name__ == '__main__':
    sys.exit(main())
