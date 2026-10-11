"""Official Qwen-MT settings, stored locally with a masked API key field."""

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QMessageBox, QSpinBox, QVBoxLayout,
)

from api.translation_client import TranslationSettings
from core.config import DEFAULT_TRANSLATION_SETTINGS, TRANSLATION_MODELS, TRANSLATION_REGIONS


class TranslationSettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        values = {**DEFAULT_TRANSLATION_SETTINGS, **settings}
        self.setWindowTitle("翻译设置（Qwen-MT）")
        self.setMinimumWidth(450)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.api_key_edit = QLineEdit(values["translation_api_key"])
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.region_combo = QComboBox()
        for name, code in TRANSLATION_REGIONS.items():
            self.region_combo.addItem(name, code)
        self.region_combo.setCurrentIndex(max(0, self.region_combo.findData(values["translation_region"])))
        self.workspace_edit = QLineEdit(values["translation_workspace_id"])
        self.workspace_edit.setPlaceholderText("可选；留空使用该地域的官方公共地址")
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems(TRANSLATION_MODELS)
        self.model_combo.setCurrentText(values["translation_model"])
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 20)
        self.batch_spin.setValue(values["translation_batch_size"])
        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 20)
        self.concurrent_spin.setValue(values["translation_max_concurrent"])
        self.rate_spin = QSpinBox()
        self.rate_spin.setRange(1, 6000)
        self.rate_spin.setSuffix(" 次/分钟")
        self.rate_spin.setValue(values["translation_rate_limit_per_minute"])
        for label, control in (
            ("API Key:", self.api_key_edit), ("官方地域:", self.region_combo),
            ("业务空间 ID:", self.workspace_edit), ("模型:", self.model_combo),
            ("每批条数:", self.batch_spin), ("并发数:", self.concurrent_spin),
            ("请求频率:", self.rate_spin),
        ):
            form.addRow(label, control)
        layout.addLayout(form)
        info = QLabel("每批原文最多 2,000 字符；单条超限时完整发送。\n"
                      "API Key 明文保存在本机 settings.json 中，界面遮罩显示。\n"
                      "翻译限流独立于转录；中文字幕在本地繁转简，无需密钥。")
        info.setWordWrap(True)
        layout.addWidget(info)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        try:
            TranslationSettings.from_dict(self.get_settings()).validate(require_key=False)
        except ValueError as exc:
            QMessageBox.warning(self, "翻译设置无效", str(exc))
            return
        self.accept()

    def get_settings(self):
        return {
            "translation_api_key": self.api_key_edit.text().strip(),
            "translation_region": self.region_combo.currentData(),
            "translation_workspace_id": self.workspace_edit.text().strip(),
            "translation_model": self.model_combo.currentText().strip(),
            "translation_batch_size": self.batch_spin.value(),
            "translation_max_concurrent": self.concurrent_spin.value(),
            "translation_rate_limit_per_minute": self.rate_spin.value(),
        }
