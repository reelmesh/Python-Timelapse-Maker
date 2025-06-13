# --- START OF FILE main_gui.py ---

import sys
import re
import json
from pathlib import Path
import subprocess
import shlex
import time
import os

from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QPushButton, QLabel, QLineEdit, QFileDialog,
                             QComboBox, QProgressBar, QTextEdit, QListWidget, QTreeWidget,
                             QTreeWidgetItem,  # Added QTreeWidget, QTreeWidgetItem
                             QSpinBox, QDoubleSpinBox, QGroupBox, QSizePolicy)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QSettings

# Assuming timelapse_engine.py is in the same directory or Python path
from timelapse_engine import (
    find_potential_sequence_dirs,
    generate_ffmpeg_commands_for_sequences_in_dir,
    count_total_sequences_in_paths,
    ENGINE_DEFAULT_FILENAME_PREFIX,
    ENGINE_DEFAULT_FILENAME_SUFFIX
)

# --- GUI Default Configuration ---
DEFAULT_PARENT_IMAGE_DIR = Path("timelapse_projects")
DEFAULT_OUTPUT_DIR = Path("timelapses_output")  # Changed from your previous script's default
DEFAULT_INPUT_FPS = 24.0
DEFAULT_OUTPUT_FPS = 24.0
DEFAULT_CODEC_ID = "h264_mp4"
DEFAULT_X264_X265_PRESET = "medium"
DEFAULT_PRORES_PROFILE_KEY = "HQ"
DEFAULT_DNXHR_PROFILE_KEY = "dnxhr_hq"  # Default to a profile string
DEFAULT_VP9_DEADLINE = "good"
DEFAULT_VP9_CPU_USED = 1


class FFmpegWorker(QThread):
    progress_update = pyqtSignal(int, int)
    log_message = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, ffmpeg_cmd_list, output_path, total_frames):
        super().__init__()
        self.ffmpeg_cmd_list = [str(arg) for arg in ffmpeg_cmd_list]
        self.output_path = output_path
        self.total_frames = total_frames
        self.process = None

    def run(self):  # Using the more complete version of run_ffmpeg_command logic
        self.log_message.emit(f"Starting FFmpeg for: {self.output_path.name} ({self.total_frames} frames)")
        # For debugging the command:
        # self.log_message.emit(f"  CMD: {' '.join(shlex.quote(str(arg)) for arg in self.ffmpeg_cmd_list)}")

        progress_arg = "-progress"
        progress_pipe = "pipe:1"
        ffmpeg_cmd_with_progress = []
        try:
            i_index = self.ffmpeg_cmd_list.index('-i')
            ffmpeg_cmd_with_progress = self.ffmpeg_cmd_list[:i_index] + [progress_arg,
                                                                         progress_pipe] + self.ffmpeg_cmd_list[i_index:]
        except ValueError:
            ffmpeg_cmd_with_progress = [self.ffmpeg_cmd_list[0], progress_arg, progress_pipe] + self.ffmpeg_cmd_list[1:]
            self.log_message.emit(
                "Warning: Could not reliably place -progress option using -i; attempting fallback placement.")

        try:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            self.process = subprocess.Popen(
                ffmpeg_cmd_with_progress,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, universal_newlines=True,
                creationflags=creation_flags
            )
            while True:
                if self.process.stdout is None: break
                line = self.process.stdout.readline()
                if not line and self.process.poll() is not None: break
                if line:
                    line = line.strip()
                    if '=' in line:
                        try:
                            key, value = line.split('=', 1)
                            key = key.strip();
                            value = value.strip()
                            if key == "frame":
                                self.progress_update.emit(int(value), self.total_frames)
                            elif key == "progress" and value == "end":
                                self.progress_update.emit(self.total_frames, self.total_frames)
                                break
                        except ValueError:
                            self.log_message.emit(f"Warning: Could not parse progress line: {line}")
                        except Exception as e_parse:
                            self.log_message.emit(f"Warning: Error parsing progress line '{line}': {e_parse}")

            stdout_final, stderr_final = self.process.communicate(timeout=120)  # Increased timeout further

            if self.process.returncode == 0:
                self.log_message.emit(f"Successfully created: {str(self.output_path)}")
                if stderr_final and stderr_final.strip() and "deprecated pixel format" not in stderr_final.lower():  # Log significant stderr
                    self.log_message.emit(f"FFmpeg messages for {self.output_path.name}:\n{stderr_final.strip()}")
                self.finished.emit(True, str(self.output_path))
            else:
                self.log_message.emit(
                    f"ERROR: FFmpeg command failed for {self.output_path} (exit code {self.process.returncode})")
                if stderr_final: self.log_message.emit(f"FFmpeg stderr:\n{stderr_final.strip()}")
                self.finished.emit(False, str(self.output_path))
        except FileNotFoundError:
            self.log_message.emit("Error: ffmpeg command not found. Is FFmpeg installed and in your PATH?")
            self.finished.emit(False, str(self.output_path))
        except subprocess.TimeoutExpired:
            self.log_message.emit(f"Error: FFmpeg command timed out for {self.output_path}")
            if self.process and self.process.poll() is None: self.process.kill(); self.process.communicate()
            self.finished.emit(False, str(self.output_path))
        except Exception as e:
            self.log_message.emit(f"Critical error in FFmpeg worker thread for {self.output_path}: {e}")
            self.finished.emit(False, str(self.output_path))
        finally:
            if self.process and self.process.poll() is None:
                self.log_message.emit(f"Ensuring FFmpeg process termination for {self.output_path.name}...")
                self.process.kill();
                self.process.communicate()


class TimelapseApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python Timelapse Maker GUI v0.4")
        self.setGeometry(100, 100, 950, 850)  # Adjusted size

        self.codec_data_list_for_ui = [  # Ensure this matches your engine's expectations if separated
            {"id": "h264_mp4", "name": "H.264 / .mp4 (Compat)", "base_codec": "libx264",
             "hw_variants": {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "amf": "h264_amf"}, "ext": ".mp4",
             "pix_fmt": "yuv420p", "quality_type": "crf", "default_crf": 23,
             "presets": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower",
                         "veryslow"], "default_preset_key": DEFAULT_X264_X265_PRESET, "hw_quality_type": "cq",
             "hw_default_cq": 23, "hw_presets": {
                "nvenc": ["default", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "slow", "medium", "fast", "hp", "hq",
                          "bd", "ll", "llhq", "llhp", "lossless", "losslesshp"], "qsv": None,
                "amf": ["ultrafast", "fast", "balanced", "quality", "highquality"]}},
            {"id": "h265_mp4", "name": "H.265 (HEVC) / .mp4", "base_codec": "libx265",
             "hw_variants": {"nvenc": "hevc_nvenc", "qsv": "hevc_qsv", "amf": "hevc_amf"}, "ext": ".mp4",
             "pix_fmt": "yuv420p", "quality_type": "crf", "default_crf": 28,
             "presets": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower",
                         "veryslow"], "default_preset_key": DEFAULT_X264_X265_PRESET, "hw_quality_type": "cq",
             "hw_default_cq": 28, "hw_presets": {
                "nvenc": ["default", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "slow", "medium", "fast", "hp", "hq",
                          "bd", "ll", "llhq", "llhp", "lossless", "losslesshp"], "qsv": None,
                "amf": ["ultrafast", "fast", "balanced", "quality", "highquality"]}},
            {"id": "h265_mkv", "name": "H.265 (HEVC) / .mkv", "base_codec": "libx265",
             "hw_variants": {"nvenc": "hevc_nvenc", "qsv": "hevc_qsv", "amf": "hevc_amf"}, "ext": ".mkv",
             "pix_fmt": "yuv420p", "quality_type": "crf", "default_crf": 28,
             "presets": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower",
                         "veryslow"], "default_preset_key": DEFAULT_X264_X265_PRESET, "hw_quality_type": "cq",
             "hw_default_cq": 28, "hw_presets": {
                "nvenc": ["default", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "slow", "medium", "fast", "hp", "hq",
                          "bd", "ll", "llhq", "llhp", "lossless", "losslesshp"], "qsv": None,
                "amf": ["ultrafast", "fast", "balanced", "quality", "highquality"]}},
            {"id": "vp9_webm", "name": "VP9 / .webm (Web)", "base_codec": "libvpx-vp9", "hw_variants": {},
             "ext": ".webm", "pix_fmt": "yuv420p", "quality_type": "crf_vp9", "default_crf": 31,
             "deadlines": ["realtime", "good", "best"], "default_deadline_key": DEFAULT_VP9_DEADLINE,
             "default_cpu_used": DEFAULT_VP9_CPU_USED},
            {"id": "vp9_mkv", "name": "VP9 / .mkv", "base_codec": "libvpx-vp9", "hw_variants": {}, "ext": ".mkv",
             "pix_fmt": "yuv420p", "quality_type": "crf_vp9", "default_crf": 31,
             "deadlines": ["realtime", "good", "best"], "default_deadline_key": DEFAULT_VP9_DEADLINE,
             "default_cpu_used": DEFAULT_VP9_CPU_USED},
            {"id": "prores_ks_hq_mov", "name": "ProRes 422 HQ / .mov", "base_codec": "prores_ks", "hw_variants": {},
             "ext": ".mov", "pix_fmt": "yuv422p10le", "quality_type": "prores_profile",
             "prores_profiles_map": {"Proxy": 0, "LT": 1, "Standard": 2, "HQ": 3, "4444": 4, "4444XQ": 5},
             "default_profile_key": DEFAULT_PRORES_PROFILE_KEY},
            {"id": "prores_ks_std_mov", "name": "ProRes 422 Standard / .mov", "base_codec": "prores_ks",
             "hw_variants": {}, "ext": ".mov", "pix_fmt": "yuv422p10le", "quality_type": "prores_profile",
             "prores_profiles_map": {"Proxy": 0, "LT": 1, "Standard": 2, "HQ": 3, "4444": 4, "4444XQ": 5},
             "default_profile_key": "Standard"},
            {"id": "dnxhr_hqx_mov", "name": "DNxHR HQX (12-bit) / .mov", "base_codec": "dnxhd", "hw_variants": {},
             "ext": ".mov", "pix_fmt": "yuv422p10le", "quality_type": "dnx_profile",
             "dnx_profiles_list": ["dnxhr_hqx", "dnxhr_hq", "dnxhr_sq", "dnxhr_lb"],
             "default_dnx_profile_key": "dnxhr_hqx"},
            {"id": "dnxhr_hq_mov", "name": "DNxHR HQ (8-bit) / .mov", "base_codec": "dnxhd", "hw_variants": {},
             "ext": ".mov", "pix_fmt": "yuv420p", "quality_type": "dnx_profile",
             "dnx_profiles_list": ["dnxhr_hqx", "dnxhr_hq", "dnxhr_sq", "dnxhr_lb"],
             "default_dnx_profile_key": DEFAULT_DNXHR_PROFILE_KEY}
        ]
        self.scale_options_map_for_ui = {
            "original": {"desc": "Original (no scaling)", "filter": ""},
            "percentage": {"desc": "Percentage of original",
                           "filter_template": "scale=w=trunc(iw*{val}/100/2)*2:h=-2:flags=lanczos"},
            "1080p": {"desc": "1080p (1920xH)", "filter": "scale=1920:-2:flags=lanczos"},
            "720p": {"desc": "720p (1280xH)", "filter": "scale=1280:-2:flags=lanczos"},
            "custom": {"desc": "Custom WxH", "filter_template": "scale={w}:{h}:flags=lanczos"}
        }
        self.presets_dir = Path(".") / "timelapse_presets"  # Changed name
        self.presets_dir.mkdir(parents=True, exist_ok=True)
        self.settings = QSettings("My Timelapse App", "TimelapseMakerGUI")  # More specific org/app names
        self.current_theme = self.settings.value("theme", "light", type=str)  # Specify type for QSettings

        self.init_ui()

        self.ffmpeg_workers = []
        self.dirs_for_current_batch = []
        self.current_batch_dir_index = 0
        self.current_batch_sequence_generator = None
        self.processed_sequences_in_batch_count = 0  # For overall progress

    def init_ui(self):
        main_layout = QVBoxLayout()

        top_bar_layout = QHBoxLayout()
        top_bar_layout.addStretch()
        self.theme_toggle_button = QPushButton("Switch to Dark Mode")
        self.theme_toggle_button.setCheckable(True)
        self.theme_toggle_button.clicked.connect(self.toggle_theme)
        top_bar_layout.addWidget(self.theme_toggle_button)
        main_layout.addLayout(top_bar_layout)

        dir_group = QGroupBox("Input / Output Directories");
        dir_layout = QGridLayout();
        dir_layout.addWidget(QLabel("Parent Timelapse Directory:"), 0, 0);
        self.parent_dir_edit = QLineEdit(str(DEFAULT_PARENT_IMAGE_DIR));
        dir_layout.addWidget(self.parent_dir_edit, 0, 1);
        self.parent_dir_browse_btn = QPushButton("Browse...");
        self.parent_dir_browse_btn.clicked.connect(self.browse_parent_dir);
        dir_layout.addWidget(self.parent_dir_browse_btn, 0, 2);
        dir_layout.addWidget(QLabel("Main Output Directory:"), 1, 0);
        self.output_dir_edit = QLineEdit(str(DEFAULT_OUTPUT_DIR));
        dir_layout.addWidget(self.output_dir_edit, 1, 1);
        self.output_dir_browse_btn = QPushButton("Browse...");
        self.output_dir_browse_btn.clicked.connect(self.browse_output_dir);
        dir_layout.addWidget(self.output_dir_browse_btn, 1, 2);
        dir_group.setLayout(dir_layout);
        main_layout.addWidget(dir_group)

        filename_group = QGroupBox("Filename & Sequence Detection");
        filename_layout = QGridLayout();
        filename_layout.addWidget(QLabel("Filename Prefix:"), 0, 0);
        self.filename_prefix_edit = QLineEdit(ENGINE_DEFAULT_FILENAME_PREFIX);
        filename_layout.addWidget(self.filename_prefix_edit, 0, 1);
        filename_layout.addWidget(QLabel("Filename Suffix:"), 0, 2);
        self.filename_suffix_edit = QLineEdit(ENGINE_DEFAULT_FILENAME_SUFFIX);
        filename_layout.addWidget(self.filename_suffix_edit, 0, 3);
        filename_layout.addWidget(QLabel("Output File Base Name (Optional):"), 1, 0);
        self.output_basename_edit = QLineEdit();
        self.output_basename_edit.setPlaceholderText("Default: <Input_Directory_Name>");
        filename_layout.addWidget(self.output_basename_edit, 1, 1, 1, 3);
        filename_group.setLayout(filename_layout);
        main_layout.addWidget(filename_group)

        core_settings_group = QGroupBox("Core Timelapse Settings");
        core_settings_layout = QHBoxLayout();
        self.input_fps_spin = QDoubleSpinBox();
        self.input_fps_spin.setSuffix(" fps");
        self.input_fps_spin.setValue(DEFAULT_INPUT_FPS);
        self.input_fps_spin.setMinimum(0.1);
        self.output_fps_spin = QDoubleSpinBox();
        self.output_fps_spin.setSuffix(" fps");
        self.output_fps_spin.setValue(DEFAULT_OUTPUT_FPS);
        self.output_fps_spin.setMinimum(1.0);
        core_settings_layout.addWidget(QLabel("Input FPS:"));
        core_settings_layout.addWidget(self.input_fps_spin);
        core_settings_layout.addStretch();
        core_settings_layout.addWidget(QLabel("Output FPS:"));
        core_settings_layout.addWidget(self.output_fps_spin);
        core_settings_group.setLayout(core_settings_layout);
        main_layout.addWidget(core_settings_group)

        encoding_group = QGroupBox("Encoding Settings");
        encoding_layout = QGridLayout();
        self.hw_accel_label = QLabel("Hardware Acceleration:");
        self.hw_accel_combo = QComboBox();
        self.hw_accel_options_display = ["None (Software Encoding)", "NVIDIA NVENC", "Intel QSV", "AMD AMF"];
        self.hw_accel_combo.addItems(self.hw_accel_options_display);
        encoding_layout.addWidget(self.hw_accel_label, 0, 0);
        encoding_layout.addWidget(self.hw_accel_combo, 0, 1, 1, 3);
        self.codec_label = QLabel("Base Codec/Format:");
        self.codec_combo = QComboBox();
        for opt in self.codec_data_list_for_ui: self.codec_combo.addItem(opt["name"], userData=opt)
        encoding_layout.addWidget(self.codec_label, 1, 0);
        encoding_layout.addWidget(self.codec_combo, 1, 1, 1, 3);
        self.quality_label = QLabel("Quality (CRF/CQ/Profile):");
        self.quality_spin = QSpinBox();
        self.quality_spin.setRange(0, 63);
        self.quality_combo = QComboBox();
        self.preset_label = QLabel("Preset/Deadline/Speed:");
        self.preset_combo = QComboBox();
        encoding_layout.addWidget(self.quality_label, 2, 0);
        encoding_layout.addWidget(self.quality_spin, 2, 1);
        encoding_layout.addWidget(self.quality_combo, 2, 2, 1, 2);
        encoding_layout.addWidget(self.preset_label, 3, 0);
        encoding_layout.addWidget(self.preset_combo, 3, 1, 1, 3);
        self.codec_combo.currentIndexChanged.connect(self.update_dynamic_codec_options_ui);
        self.hw_accel_combo.currentIndexChanged.connect(self.update_dynamic_codec_options_ui);
        encoding_group.setLayout(encoding_layout);
        main_layout.addWidget(encoding_group)

        scaling_group = QGroupBox("Resolution Scaling");
        scaling_layout = QHBoxLayout();
        self.scale_type_combo = QComboBox()
        for scale_key, opt_data in self.scale_options_map_for_ui.items(): self.scale_type_combo.addItem(
            opt_data["desc"], userData=scale_key)
        self.scale_percentage_spin = QSpinBox();
        self.scale_percentage_spin.setRange(1, 200);
        self.scale_percentage_spin.setValue(50);
        self.scale_percentage_spin.setSuffix("%");
        self.scale_custom_width_edit = QLineEdit("1920");
        self.scale_custom_height_edit = QLineEdit("1080");
        self.custom_w_label = QLabel("W:");
        self.custom_h_label = QLabel("H:");
        scaling_layout.addWidget(QLabel("Type:"));
        scaling_layout.addWidget(self.scale_type_combo);
        scaling_layout.addWidget(self.scale_percentage_spin);
        scaling_layout.addWidget(self.custom_w_label);
        scaling_layout.addWidget(self.scale_custom_width_edit);
        scaling_layout.addWidget(self.custom_h_label);
        scaling_layout.addWidget(self.scale_custom_height_edit);
        self.scale_type_combo.currentIndexChanged.connect(self.update_scaling_options_ui);
        scaling_group.setLayout(scaling_layout);
        main_layout.addWidget(scaling_group)

        # --- Preset Buttons ---
        preset_layout = QHBoxLayout()  # Define preset_layout before using it
        self.load_preset_button = QPushButton("Load Preset");
        self.load_preset_button.clicked.connect(self.load_preset_action)
        self.save_preset_button = QPushButton("Save Settings as Preset");
        self.save_preset_button.clicked.connect(self.save_preset_action)
        preset_layout.addWidget(self.load_preset_button);
        preset_layout.addWidget(self.save_preset_button)
        main_layout.addLayout(preset_layout)  # Now add it

        action_layout = QHBoxLayout();
        self.scan_button = QPushButton("Scan for Sequences");
        self.scan_button.clicked.connect(self.scan_directories_action);
        self.start_button = QPushButton("Start Batch Processing");
        self.start_button.setEnabled(False);
        self.start_button.clicked.connect(self.start_batch_action);
        action_layout.addWidget(self.scan_button);
        action_layout.addWidget(self.start_button);
        main_layout.addLayout(action_layout)
        main_layout.addWidget(QLabel("Found Sequence Directories / Sequences:"));
        self.dir_tree_widget = QTreeWidget()  # Changed from QListWidget
        self.dir_tree_widget.setHeaderLabels(["Directory / Sequence", "Frames (approx)"])
        main_layout.addWidget(self.dir_tree_widget)
        self.current_sequence_progress_bar = QProgressBar();
        self.current_sequence_progress_bar.setTextVisible(True);
        main_layout.addWidget(QLabel("Current Sequence Progress:"));
        main_layout.addWidget(self.current_sequence_progress_bar)
        self.overall_batch_progress_bar = QProgressBar();
        self.overall_batch_progress_bar.setTextVisible(True);
        main_layout.addWidget(QLabel("Overall Batch Progress:"));
        main_layout.addWidget(self.overall_batch_progress_bar)
        main_layout.addWidget(QLabel("Log:"));
        self.log_text_edit = QTextEdit();
        self.log_text_edit.setReadOnly(True);
        main_layout.addWidget(self.log_text_edit)

        self.setLayout(main_layout)
        self.update_scaling_options_ui()
        self.update_dynamic_codec_options_ui()
        self.apply_theme(self.current_theme)

    def apply_theme(self, theme_name: str):
        if theme_name == "dark":
            script_dir = Path(__file__).resolve().parent
            stylesheet_path = script_dir / "dark_theme.qss"
            if stylesheet_path.exists():
                with open(stylesheet_path, "r") as f:
                    QApplication.instance().setStyleSheet(f.read())
                self.theme_toggle_button.setText("Switch to Light Mode");
                self.theme_toggle_button.setChecked(True)
                self.log("Dark theme applied.")
            else:
                self.log(f"Warning: dark_theme.qss not found. Using default.");
                QApplication.instance().setStyleSheet("")
                self.theme_toggle_button.setText("Switch to Dark Mode (Sheet Missing)");
                self.theme_toggle_button.setChecked(False)
        else:
            QApplication.instance().setStyleSheet("");
            self.theme_toggle_button.setText("Switch to Dark Mode");
            self.theme_toggle_button.setChecked(False)
            self.log("Light theme (default) applied.")
        self.settings.setValue("theme", theme_name)

    def toggle_theme(self):
        self.current_theme = "dark" if self.theme_toggle_button.isChecked() else "light"
        self.apply_theme(self.current_theme)

    def log(self, message: str):
        if hasattr(self, 'log_text_edit') and self.log_text_edit is not None:
            self.log_text_edit.append(str(message)); QApplication.processEvents()
        else:
            print(f"LOG (UI not ready): {message}")

    def browse_parent_dir(self):
        initial_dir = self.parent_dir_edit.text() or str(DEFAULT_PARENT_IMAGE_DIR)
        dir_path = QFileDialog.getExistingDirectory(self, "Select Parent Timelapse Directory", initial_dir,
                                                    options=QFileDialog.Option.DontUseNativeDialog | QFileDialog.Option.ShowDirsOnly)
        if dir_path: self.parent_dir_edit.setText(dir_path)

    def browse_output_dir(self):
        initial_dir = self.output_dir_edit.text() or str(DEFAULT_OUTPUT_DIR)
        dir_path = QFileDialog.getExistingDirectory(self, "Select Main Output Directory", initial_dir,
                                                    options=QFileDialog.Option.DontUseNativeDialog | QFileDialog.Option.ShowDirsOnly)
        if dir_path: self.output_dir_edit.setText(dir_path);
        try:
            Path(dir_path).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log(f"Error ensuring output directory exists: {e}")

    def update_dynamic_codec_options_ui(self):
        self.quality_spin.hide();
        self.quality_combo.hide();
        self.preset_combo.hide()
        self.quality_label.setText("Quality:");
        self.preset_label.setText("Preset/Speed:")
        selected_codec_idx = self.codec_combo.currentIndex()
        if selected_codec_idx < 0: return
        chosen_base_codec_data = self.codec_combo.itemData(selected_codec_idx)
        if not chosen_base_codec_data: return
        selected_hwaccel_type_idx = self.hw_accel_combo.currentIndex()
        hwaccel_type = ["none", "nvenc", "qsv", "amf"][selected_hwaccel_type_idx]
        is_hw_active = False;
        actual_video_codec = chosen_base_codec_data["base_codec"]
        if hwaccel_type != "none" and hwaccel_type in chosen_base_codec_data.get("hw_variants", {}):
            hw_variant = chosen_base_codec_data["hw_variants"].get(hwaccel_type)
            if hw_variant: is_hw_active = True; actual_video_codec = hw_variant
        if is_hw_active:
            self.quality_label.setText(f"CQ/QP ({actual_video_codec}):");
            self.quality_spin.setRange(0, 51);
            self.quality_spin.setValue(chosen_base_codec_data.get("hw_default_cq", 23));
            self.quality_spin.show()
            hw_presets = chosen_base_codec_data.get("hw_presets", {}).get(hwaccel_type)
            if hw_presets:
                self.preset_label.setText(f"HW Preset ({actual_video_codec}):");
                self.preset_combo.clear();
                self.preset_combo.addItems(hw_presets)
                try:
                    default_hw_preset_idx = hw_presets.index("medium") if "medium" in hw_presets else (
                        hw_presets.index("p4") if "p4" in hw_presets else len(hw_presets) // 2)
                except ValueError:
                    default_hw_preset_idx = 0
                if hw_presets: self.preset_combo.setCurrentIndex(default_hw_preset_idx)
                self.preset_combo.show()
        else:
            quality_type = chosen_base_codec_data["quality_type"]
            if quality_type == "crf":
                self.quality_label.setText(f"CRF ({actual_video_codec}):");
                self.quality_spin.setRange(0, 51);
                self.quality_spin.setValue(chosen_base_codec_data.get("default_crf", 23));
                self.quality_spin.show()
                if "presets" in chosen_base_codec_data:
                    self.preset_label.setText(f"Preset ({actual_video_codec}):");
                    self.preset_combo.clear();
                    self.preset_combo.addItems(chosen_base_codec_data["presets"])
                    default_sw_preset = chosen_base_codec_data.get("default_preset_key", DEFAULT_X264_X265_PRESET)
                    try:
                        default_sw_idx = chosen_base_codec_data["presets"].index(default_sw_preset)
                    except ValueError:
                        default_sw_idx = chosen_base_codec_data["presets"].index("medium") if "medium" in \
                                                                                              chosen_base_codec_data[
                                                                                                  "presets"] else len(
                            chosen_base_codec_data["presets"]) // 2
                    self.preset_combo.setCurrentIndex(default_sw_idx);
                    self.preset_combo.show()
            elif quality_type == "crf_vp9":
                self.quality_label.setText(f"CRF ({actual_video_codec}):");
                self.quality_spin.setRange(0, 63);
                self.quality_spin.setValue(chosen_base_codec_data.get("default_crf", 31));
                self.quality_spin.show()
                if "deadlines" in chosen_base_codec_data:
                    self.preset_label.setText(f"Deadline ({actual_video_codec}):");
                    self.preset_combo.clear();
                    self.preset_combo.addItems(chosen_base_codec_data["deadlines"])
                    default_vp9_deadline = chosen_base_codec_data.get("default_deadline_key", DEFAULT_VP9_DEADLINE)
                    try:
                        default_vp9_idx = chosen_base_codec_data["deadlines"].index(default_vp9_deadline)
                    except ValueError:
                        default_vp9_idx = 0
                    self.preset_combo.setCurrentIndex(default_vp9_idx);
                    self.preset_combo.show()
            elif quality_type == "prores_profile":
                self.quality_label.setText(f"ProRes Profile:");
                self.quality_combo.clear();
                self.quality_combo.addItems(chosen_base_codec_data["prores_profiles_map"].keys())
                default_prores_key = chosen_base_codec_data.get("default_profile_key", DEFAULT_PRORES_PROFILE_KEY)
                if default_prores_key in chosen_base_codec_data[
                    "prores_profiles_map"]: self.quality_combo.setCurrentText(default_prores_key)
                self.quality_combo.show()
            elif quality_type == "dnx_profile":
                self.quality_label.setText(f"DNxHR Profile:");
                self.quality_combo.clear();
                self.quality_combo.addItems(chosen_base_codec_data["dnx_profiles_list"])
                default_dnx_key = chosen_base_codec_data.get("default_dnx_profile_key", DEFAULT_DNXHR_PROFILE_KEY)
                if default_dnx_key in chosen_base_codec_data["dnx_profiles_list"]: self.quality_combo.setCurrentText(
                    default_dnx_key)
                self.quality_combo.show()

    def update_scaling_options_ui(self):
        selected_scale_key = self.scale_type_combo.currentData();
        if selected_scale_key is None: return
        self.scale_percentage_spin.setVisible(selected_scale_key == "percentage")
        is_custom = (selected_scale_key == "custom")
        self.custom_w_label.setVisible(is_custom);
        self.scale_custom_width_edit.setVisible(is_custom)
        self.custom_h_label.setVisible(is_custom);
        self.scale_custom_height_edit.setVisible(is_custom)

    def gather_common_settings_from_ui(self) -> dict | None:
        self.log("Gathering settings from UI...")
        try:
            main_output_dir = Path(self.output_dir_edit.text())
            if not main_output_dir.is_dir(): main_output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log(f"Error with main output directory: {e}."); return None
        settings = {"input_fps": self.input_fps_spin.value(), "output_fps": self.output_fps_spin.value(),
                    "main_output_dir": main_output_dir,
                    "filename_prefix_ui": self.filename_prefix_edit.text() or ENGINE_DEFAULT_FILENAME_PREFIX,
                    "filename_suffix_ui": self.filename_suffix_edit.text() or ENGINE_DEFAULT_FILENAME_SUFFIX,
                    "output_basename_ui": self.output_basename_edit.text().strip(),
                    "video_codec_option_name": "Unknown", "video_codec": "libx264", "base_codec": "libx264",
                    "output_extension": ".mp4", "pixel_format_final": "yuv420p", "is_crf_based": False,
                    "crf_value": None, "codec_preset": None, "prores_profile_val": None, "dnx_bitrate_or_profile": None,
                    "vp9_cpu_used": None, "prores_profiles_map": None, "hwaccel_type": "none", "hw_cq_value": None,
                    "hw_preset": None, "scale_filter_string": "", "resolution_desc": "Original"}
        selected_codec_idx_ui = self.codec_combo.currentIndex()
        if selected_codec_idx_ui < 0: self.log("Error: No base codec selected."); return None
        chosen_base_codec_data = self.codec_combo.itemData(selected_codec_idx_ui)
        if not chosen_base_codec_data: self.log("Error: No data for selected codec."); return None
        settings.update({"video_codec_option_name": chosen_base_codec_data["name"],
                         "base_codec": chosen_base_codec_data["base_codec"],
                         "video_codec": chosen_base_codec_data["base_codec"],
                         "output_extension": chosen_base_codec_data["ext"],
                         "pixel_format_final": chosen_base_codec_data["pix_fmt"]})
        if "prores_profiles_map" in chosen_base_codec_data: settings["prores_profiles_map"] = chosen_base_codec_data[
            "prores_profiles_map"]
        selected_hwaccel_type_idx = self.hw_accel_combo.currentIndex()
        hwaccel_type = ["none", "nvenc", "qsv", "amf"][
            selected_hwaccel_type_idx] if selected_hwaccel_type_idx < 4 else "none"
        settings["hwaccel_type"] = hwaccel_type
        is_hw_active = False
        if hwaccel_type != "none" and hwaccel_type in chosen_base_codec_data.get("hw_variants", {}):
            hw_variant = chosen_base_codec_data["hw_variants"].get(hwaccel_type)
            if hw_variant:
                settings["video_codec"] = hw_variant;
                settings["video_codec_option_name"] += f" ({hwaccel_type.upper()})";
                is_hw_active = True
                if settings["video_codec"].startswith("hevc_nvenc") and settings["pixel_format_final"] == "yuv422p10le":
                    settings["pixel_format_final"] = "p010le"
                elif settings["video_codec"].startswith("h264_nvenc") and settings["pixel_format_final"] not in [
                    "yuv420p", "nv12"]:
                    settings["pixel_format_final"] = "yuv420p"
        if is_hw_active:
            if chosen_base_codec_data.get("hw_quality_type") == "cq" and self.quality_spin.isVisible(): settings[
                "hw_cq_value"] = self.quality_spin.value()
            hw_presets_for_this_hw_type = chosen_base_codec_data.get("hw_presets", {}).get(hwaccel_type)
            if hw_presets_for_this_hw_type and self.preset_combo.isVisible() and self.preset_combo.currentText() in hw_presets_for_this_hw_type:
                settings["hw_preset"] = self.preset_combo.currentText()
        else:
            quality_type = chosen_base_codec_data["quality_type"]
            if quality_type == "crf":
                settings["is_crf_based"] = True
                if self.quality_spin.isVisible(): settings["crf_value"] = self.quality_spin.value()
                if "presets" in chosen_base_codec_data and self.preset_combo.isVisible(): settings[
                    "codec_preset"] = self.preset_combo.currentText()
            elif quality_type == "crf_vp9":
                settings["is_crf_based"] = True
                if self.quality_spin.isVisible(): settings["crf_value"] = self.quality_spin.value()
                if "deadlines" in chosen_base_codec_data and self.preset_combo.isVisible(): settings[
                    "codec_preset"] = self.preset_combo.currentText()
                settings["vp9_cpu_used"] = chosen_base_codec_data.get("default_cpu_used", DEFAULT_VP9_CPU_USED)
            elif quality_type == "prores_profile":
                if self.quality_combo.isVisible():
                    selected_profile_name = self.quality_combo.currentText()
                    if "prores_profiles_map" in chosen_base_codec_data: settings["prores_profile_val"] = \
                    chosen_base_codec_data["prores_profiles_map"].get(selected_profile_name)
                    if selected_profile_name in ["4444", "4444XQ"]: settings["pixel_format_final"] = "yuv444p10le"
            elif quality_type == "dnx_profile":
                if self.quality_combo.isVisible():
                    settings["dnx_bitrate_or_profile"] = self.quality_combo.currentText()
                    if settings["dnx_bitrate_or_profile"] == "dnxhr_hqx":
                        settings["pixel_format_final"] = "yuv422p10le"
                    elif settings["dnx_bitrate_or_profile"] in ["dnxhr_hq", "dnxhr_sq",
                                                                "dnxhr_lb"] and chosen_base_codec_data.get(
                        "pix_fmt") != "yuv420p":
                        settings["pixel_format_final"] = "yuv422p"
        selected_scale_key = self.scale_type_combo.currentData()
        if selected_scale_key and selected_scale_key in self.scale_options_map_for_ui:
            chosen_scale_data = self.scale_options_map_for_ui[selected_scale_key]
            settings["resolution_desc"] = self.scale_type_combo.currentText()
            if selected_scale_key == "original":
                settings["scale_filter_string"] = ""
            elif selected_scale_key == "percentage":
                percentage = self.scale_percentage_spin.value()
                settings["scale_filter_string"] = chosen_scale_data["filter_template"].replace("{val}",
                                                                                               str(percentage));
                settings["resolution_desc"] = f"{percentage}% of original (approx, even dimensions)"
            elif selected_scale_key == "custom":
                w, h = self.scale_custom_width_edit.text(), self.scale_custom_height_edit.text()
                try:
                    iw, ih_str_val = int(w), h
                    if not (iw > 0 and iw % 2 == 0): raise ValueError("Width must be positive and even.")
                    if not (ih_str_val == "-2" or (ih_str_val.isdigit() and int(ih_str_val) > 0 and int(
                        ih_str_val) % 2 == 0)): raise ValueError("Height must be -2 or a positive even integer.")
                    settings["scale_filter_string"] = chosen_scale_data["filter_template"].replace("{w}", w).replace(
                        "{h}", h);
                    settings["resolution_desc"] = f"Custom {w}x{h if h != '-2' else '(auto_H)'}"
                except ValueError as e_scale:
                    self.log(f"Warning: Invalid custom scale: {e_scale}. No scaling."); settings[
                        "scale_filter_string"] = ""; settings["resolution_desc"] = "Original (Custom Err)"
            else:
                settings["scale_filter_string"] = chosen_scale_data.get("filter", "")
        else:
            self.log("Warning: No valid scale type. No scaling."); settings["scale_filter_string"] = ""; settings[
                "resolution_desc"] = "Original (No Scale Sel)"
        self.log(f"Settings gathered. Codec: {settings.get('video_codec')}, Res: {settings.get('resolution_desc')}")
        return settings

    def gather_ui_state_for_preset(self) -> dict:
        state = {"input_fps": self.input_fps_spin.value(), "output_fps": self.output_fps_spin.value(),
                 "output_dir_str": self.output_dir_edit.text(),
                 "hwaccel_choice_idx": self.hw_accel_combo.currentIndex(),
                 "codec_choice_idx": self.codec_combo.currentIndex(), "quality_spin_value": self.quality_spin.value(),
                 "quality_combo_text": self.quality_combo.currentText(),
                 "preset_combo_text": self.preset_combo.currentText(),
                 "scale_type_combo_idx": self.scale_type_combo.currentIndex(),
                 "scale_percentage_value": self.scale_percentage_spin.value(),
                 "scale_custom_width_text": self.scale_custom_width_edit.text(),
                 "scale_custom_height_text": self.scale_custom_height_edit.text()}
        return state

    def apply_settings_to_ui(self, settings_to_load: dict):
        self.log("Applying loaded preset to UI...")
        try:
            self.input_fps_spin.setValue(settings_to_load.get("input_fps", DEFAULT_INPUT_FPS))
            self.output_fps_spin.setValue(settings_to_load.get("output_fps", DEFAULT_OUTPUT_FPS))
            self.output_dir_edit.setText(settings_to_load.get("output_dir_str", str(DEFAULT_OUTPUT_DIR)))
            self.hw_accel_combo.setCurrentIndex(settings_to_load.get("hwaccel_choice_idx", 0))
            self.codec_combo.setCurrentIndex(settings_to_load.get("codec_choice_idx", 0))
            self.update_dynamic_codec_options_ui();
            QApplication.processEvents()
            if self.quality_spin.isVisible() and settings_to_load.get(
                "quality_spin_value") is not None: self.quality_spin.setValue(settings_to_load["quality_spin_value"])
            if self.quality_combo.isVisible() and settings_to_load.get(
                "quality_combo_text"): self.quality_combo.setCurrentText(settings_to_load["quality_combo_text"])
            if self.preset_combo.isVisible() and settings_to_load.get(
                "preset_combo_text"): self.preset_combo.setCurrentText(settings_to_load["preset_combo_text"])
            self.scale_type_combo.setCurrentIndex(settings_to_load.get("scale_type_combo_idx", 0))
            self.update_scaling_options_ui();
            QApplication.processEvents()
            if self.scale_percentage_spin.isVisible() and settings_to_load.get(
                "scale_percentage_value") is not None: self.scale_percentage_spin.setValue(
                settings_to_load["scale_percentage_value"])
            if self.scale_custom_width_edit.isVisible() and settings_to_load.get(
                "scale_custom_width_text"): self.scale_custom_width_edit.setText(
                settings_to_load["scale_custom_width_text"])
            if self.scale_custom_height_edit.isVisible() and settings_to_load.get(
                "scale_custom_height_text"): self.scale_custom_height_edit.setText(
                settings_to_load["scale_custom_height_text"])
            self.log("Preset applied to UI.")
        except Exception as e:
            self.log(f"Error applying preset: {e}")

    def save_preset_action(self):
        current_ui_state = self.gather_ui_state_for_preset()
        if not current_ui_state: self.log("Could not gather settings to save."); return
        dialog = QFileDialog(self, "Save Preset As", str(self.presets_dir), "JSON Presets (*.json)")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave);
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True);
        dialog.setDefaultSuffix("json")
        if dialog.exec():
            file_path_str = dialog.selectedFiles()[0];
            file_path = Path(file_path_str)
            try:
                with open(file_path, "w") as f:
                    json.dump(current_ui_state, f, indent=4)
                self.log(f"Preset saved to: {file_path}")
            except Exception as e:
                self.log(f"Error saving preset: {e}")

    def load_preset_action(self):
        file_path_str, _ = QFileDialog.getOpenFileName(self, "Load Preset", str(self.presets_dir),
                                                       "JSON Presets (*.json)",
                                                       options=QFileDialog.Option.DontUseNativeDialog)
        if file_path_str:
            file_path = Path(file_path_str)
            try:
                with open(file_path, "r") as f:
                    loaded_ui_state = json.load(f)
                self.apply_settings_to_ui(loaded_ui_state)
            except Exception as e:
                self.log(f"Error loading preset: {e}")

    def scan_directories_action(self):  # Modified for QTreeWidget
        self.log("Scanning for sequence directories...")
        parent_dir = Path(self.parent_dir_edit.text())
        current_prefix = self.filename_prefix_edit.text() or ENGINE_DEFAULT_FILENAME_PREFIX
        current_suffix = self.filename_suffix_edit.text() or ENGINE_DEFAULT_FILENAME_SUFFIX

        self.dir_tree_widget.clear()  # Use tree widget
        self.dirs_to_process_cache = find_potential_sequence_dirs(parent_dir, current_prefix, current_suffix)

        if self.dirs_to_process_cache:
            for parent_dir_path in self.dirs_to_process_cache:
                parent_item = QTreeWidgetItem(self.dir_tree_widget, [str(parent_dir_path.name)])
                parent_item.setData(0, Qt.ItemDataRole.UserRole, {"type": "parent_dir", "path": parent_dir_path})
                parent_item.setFlags(parent_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                parent_item.setCheckState(0, Qt.CheckState.Checked)  # Auto-check parent

                # Mock generating sequence items; in real use, call engine to find sequences within parent_dir_path
                # sequences_info = get_sequence_details_for_directory(parent_dir_path, current_prefix, current_suffix)
                # For now, let's assume generate_ffmpeg_commands_for_sequences_in_dir can be used for this
                # This is a bit inefficient for just listing but will work for now.
                # A dedicated engine function would be better.
                temp_settings_for_scan = self.gather_common_settings_from_ui()  # Need some settings for the generator
                if not temp_settings_for_scan: temp_settings_for_scan = {}  # Fallback

                try:
                    for _, output_p, frames in generate_ffmpeg_commands_for_sequences_in_dir(parent_dir_path,
                                                                                             current_prefix,
                                                                                             current_suffix,
                                                                                             temp_settings_for_scan):
                        # Extract sequence start number from a dummy output_path for display
                        seq_start_num_match = re.search(r"seq(\d+)", output_p.name)
                        seq_start_display = seq_start_num_match.group(1) if seq_start_num_match else "UnknownSeq"

                        child_item = QTreeWidgetItem(parent_item,
                                                     [f"  Sequence starting ~{seq_start_display}", str(frames)])
                        # Store enough info to re-identify this exact sequence for processing
                        child_item.setData(0, Qt.ItemDataRole.UserRole, {
                            "type": "sequence",
                            "parent_path": parent_dir_path,
                            "start_number_str": seq_start_display,  # This is an approx for display
                            "num_frames": frames,
                            # "image_pattern": ... # The engine will re-determine this
                        })
                        child_item.setFlags(child_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        child_item.setCheckState(0, Qt.CheckState.Checked)
                except Exception as e:
                    self.log(f"Error scanning sequences in {parent_dir_path}: {e}")
                parent_item.setExpanded(True)
            self.log(f"Found {len(self.dirs_to_process_cache)} potential directory(s) with sequences.")
            self.start_button.setEnabled(True)
        else:
            self.log(f"No sequence directories found in '{parent_dir}'."); self.start_button.setEnabled(False)

    def start_batch_action(self):  # Modified for QTreeWidget
        self.log(f"Start batch action triggered.")
        self.dirs_for_current_batch_and_seqs = []  # List of dicts {"type": "sequence", "parent_path": ..., "start_number_str": ...}

        root = self.dir_tree_widget.invisibleRootItem()
        for i in range(root.childCount()):
            parent_item = root.child(i)
            if parent_item.checkState(0) == Qt.CheckState.Checked:  # If parent dir is checked
                parent_path = parent_item.data(0, Qt.ItemDataRole.UserRole)["path"]
                # If processing parent, assume all its children (sequences) are processed
                # OR allow individual sequence selection
                has_selected_child = False
                for j in range(parent_item.childCount()):
                    child_item = parent_item.child(j)
                    if child_item.checkState(0) == Qt.CheckState.Checked:
                        seq_data = child_item.data(0, Qt.ItemDataRole.UserRole)
                        # Ensure seq_data is the expected dictionary
                        if isinstance(seq_data, dict) and seq_data.get("type") == "sequence":
                            self.dirs_for_current_batch_and_seqs.append(seq_data)
                            has_selected_child = True

                # If parent is checked but no children, maybe log or decide if it means "process all under parent"
                if not has_selected_child and parent_item.childCount() > 0:
                    self.log(
                        f"Parent '{parent_path.name}' checked, but no individual sequences selected under it. Assuming all sequences if any exist.")
                    # Here you might re-run a lightweight scan for this parent_path to add all its sequences
                    # For now, this implies it will be skipped if no children are explicitly checked.

        if not self.dirs_for_current_batch_and_seqs:
            self.log("No sequences selected or found to process.");
            return

        self.log(f"Processing {len(self.dirs_for_current_batch_and_seqs)} selected sequence(s)...")
        self.start_button.setEnabled(False);
        self.scan_button.setEnabled(False)

        self.common_settings_for_batch = self.gather_common_settings_from_ui()
        if not self.common_settings_for_batch:
            self.log("Failed to gather settings.");
            self.start_button.setEnabled(True);
            self.scan_button.setEnabled(True);
            return

        self.current_batch_dir_index = 0  # This will now be index for dirs_for_current_batch_and_seqs
        self.current_batch_sequence_generator = None  # Not used in same way
        self.processed_sequences_in_batch_count = 0
        self.overall_batch_progress_bar.setMaximum(len(self.dirs_for_current_batch_and_seqs))
        self.overall_batch_progress_bar.setValue(0)
        self.overall_batch_progress_bar.setFormat("Overall Sequences: %v/%m")
        self.process_next_individual_sequence()

    def process_next_individual_sequence(self):  # New function to handle one sequence at a time
        if self.current_batch_dir_index >= len(self.dirs_for_current_batch_and_seqs):
            self.log("===== Batch processing fully completed. =====")
            self.scan_button.setEnabled(True);
            self.start_button.setEnabled(True if self.dir_tree_widget.topLevelItemCount() > 0 else False)
            self.overall_batch_progress_bar.setFormat("Batch Complete!");
            self.overall_batch_progress_bar.setValue(self.overall_batch_progress_bar.maximum())
            return

        sequence_data = self.dirs_for_current_batch_and_seqs[self.current_batch_dir_index]
        parent_dir = sequence_data["parent_path"]
        # The engine's generate_ffmpeg_commands will re-find the specific sequence based on start_num
        # For this, we need to ensure the engine's generator can find a *specific* sequence
        # or we pass all necessary info.
        # Let's assume generate_ffmpeg_commands_for_sequences_in_dir will find this one among others
        # if it's the first unprocessed one matching. This needs careful handling in the engine.

        self.log(
            f"--- Preparing sequence starting ~{sequence_data['start_number_str']} in {parent_dir.name} ({self.current_batch_dir_index + 1}/{len(self.dirs_for_current_batch_and_seqs)}) ---")

        # We need to get the *specific* command for *this* sequence.
        # The current generate_ffmpeg_commands_for_sequences_in_dir yields all in a directory.
        # This requires a refactor of how sequences are picked for processing.
        # Quick hack for now: iterate the generator and pick the one matching start_number_str
        # This is INEFFICIENT and should be refactored in the engine.

        ui_prefix = self.common_settings_for_batch.get("filename_prefix_ui", ENGINE_DEFAULT_FILENAME_PREFIX)
        ui_suffix = self.common_settings_for_batch.get("filename_suffix_ui", ENGINE_DEFAULT_FILENAME_SUFFIX)

        cmd_to_run, path_to_run, frames_to_run = None, None, None
        # This is a temporary workaround for selecting a specific sequence.
        # The engine should ideally have a way to get params for ONE specific sequence.
        temp_sequence_gen = generate_ffmpeg_commands_for_sequences_in_dir(parent_dir, ui_prefix, ui_suffix,
                                                                          self.common_settings_for_batch)
        for ffmpeg_cmd, output_path, total_frames in temp_sequence_gen:
            # Check if this is the sequence we want (this matching is simplistic)
            if f"seq{sequence_data['start_number_str']}" in output_path.name:
                cmd_to_run, path_to_run, frames_to_run = ffmpeg_cmd, output_path, total_frames
                break

        if cmd_to_run:
            self.log(f"  Processing sequence -> {path_to_run.name}")
            self.current_sequence_progress_bar.setFormat(f"{path_to_run.name} - %p%");
            self.current_sequence_progress_bar.setValue(0)
            worker = FFmpegWorker(cmd_to_run, path_to_run, frames_to_run)
            worker.progress_update.connect(self.update_current_sequence_progress_slot)
            worker.log_message.connect(self.log);
            worker.finished.connect(self.on_ffmpeg_worker_finished_slot)
            self.ffmpeg_workers.append(worker);
            worker.start()
        else:
            self.log(
                f"  Could not find/generate command for sequence starting ~{sequence_data['start_number_str']} in {parent_dir}. Skipping.")
            self.current_batch_dir_index += 1  # Effectively sequence index now
            self.process_next_individual_sequence()  # Try next

    def on_ffmpeg_worker_finished_slot(self, success, output_file_str):
        if success:
            self.log(f"  Sequence finished: {output_file_str}")
        else:
            self.log(f"  Sequence FAILED: {output_file_str}")

        self.processed_sequences_in_batch_count += 1
        self.overall_batch_progress_bar.setValue(self.processed_sequences_in_batch_count)

        self.current_batch_dir_index += 1  # Move to the next sequence in the list
        self.process_next_individual_sequence()

    def update_current_sequence_progress_slot(self, current_frame, total_frames):
        if self.current_sequence_progress_bar.maximum() != total_frames and total_frames > 0: self.current_sequence_progress_bar.setMaximum(
            total_frames)
        if total_frames > 0:
            self.current_sequence_progress_bar.setValue(current_frame)
        else:
            self.current_sequence_progress_bar.setValue(0)


def load_stylesheet(app_instance, qss_file_path: Path):
    if qss_file_path.exists():
        with open(qss_file_path, "r") as f:
            app_instance.setStyleSheet(f.read())
        print(f"Stylesheet '{qss_file_path.name}' loaded.")
    else:
        print(f"Warning: Stylesheet '{qss_file_path.name}' not found at {qss_file_path}.")


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    script_dir = Path(__file__).resolve().parent
    stylesheet_path = script_dir / "dark_theme.qss"
    load_stylesheet(app, stylesheet_path)
    window = TimelapseApp()
    window.show()
    sys.exit(app.exec())

# --- END OF FILE ---