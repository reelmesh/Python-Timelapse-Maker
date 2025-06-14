# --- START OF FILE main_gui.py ---

import sys
import re
import json
from pathlib import Path
import subprocess
import shlex
import time
import os
import psutil
import pyqtgraph as pg # For plotting
import monitoring_engine # NEW IMPORT
from PyQt6.QtCore import QTimer # For periodic updates
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QPushButton, QLabel, QLineEdit, QFileDialog,QDialog,
                             QComboBox, QProgressBar, QTextEdit, QListWidget, QTreeWidget,
                             QTreeWidgetItem, QCheckBox, QSplitter, QSpinBox, QDoubleSpinBox, QGroupBox, QSizePolicy, QHeaderView)
from PyQt6.QtGui import QPixmap
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

    def __init__(self, ffmpeg_cmd_list, output_path, total_frames, is_verbose_logging): # Add new param
        super().__init__()
        self.ffmpeg_cmd_list = [str(arg) for arg in ffmpeg_cmd_list]
        self.output_path = output_path
        self.total_frames = total_frames
        self.process = None
        self._is_cancelled = False # New flag
        self.is_verbose_logging = is_verbose_logging # Store it

    def cancel_task(self):
        self.log_message.emit(f"Cancellation requested for: {self.output_path.name}")
        self._is_cancelled = True
        if self.process and self.process.poll() is None: # If process is running
            try:
                self.log_message.emit(f"Terminating FFmpeg process for {self.output_path.name}...")
                self.process.terminate() # Try to terminate gracefully
                # Wait a short moment for terminate to take effect
                try:
                    self.process.wait(timeout=2) # Wait up to 2 seconds
                except subprocess.TimeoutExpired:
                    self.log_message.emit(f"FFmpeg process for {self.output_path.name} did not terminate gracefully, killing...")
                    self.process.kill() # Force kill if terminate didn't work
                self.log_message.emit(f"FFmpeg process for {self.output_path.name} termination attempt complete.")
            except Exception as e:
                self.log_message.emit(f"Error during FFmpeg process termination: {e}")
        # The run loop will also check _is_cancelled

    def run(self):
        self.log_message.emit(f"Starting FFmpeg for: {self.output_path.name} ({self.total_frames} frames)")

        progress_arg = "-progress";
        progress_pipe = "pipe:1"
        str_ffmpeg_cmd_list = [str(arg) for arg in self.ffmpeg_cmd_list]
        ffmpeg_cmd_with_progress = []
        try:
            i_index = str_ffmpeg_cmd_list.index('-i')
            ffmpeg_cmd_with_progress = str_ffmpeg_cmd_list[:i_index] + [progress_arg,
                                                                        progress_pipe] + str_ffmpeg_cmd_list[i_index:]
        except ValueError:
            ffmpeg_cmd_with_progress = [str_ffmpeg_cmd_list[0], progress_arg, progress_pipe] + str_ffmpeg_cmd_list[1:]
            self.log_message.emit("Warning: Could not reliably place -progress option using -i; attempting fallback.")

        self.log_message.emit(
            f"  Executing FFmpeg: {' '.join(shlex.quote(arg) for arg in ffmpeg_cmd_with_progress)}")  # Log the actual command

        self.process = None
        try:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            self.process = subprocess.Popen(
                ffmpeg_cmd_with_progress,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, universal_newlines=True,
                creationflags=creation_flags
            )

            if self.process.stdout is None:
                self.log_message.emit("Error: FFmpeg stdout pipe is None. Cannot read progress.")
                self.finished.emit(False, f"{str(self.output_path)} (Pipe Error)")
                return

            # Progress reading loop
            while self.process.poll() is None:  # Loop while process is running
                if self._is_cancelled:
                    self.log_message.emit(f"Cancellation signal received for {self.output_path.name}.")
                    break

                    # Try to read a line, but don't block indefinitely if Popen/pipe has issues.
                # This part is tricky without true non-blocking reads or select.
                # For now, readline() is standard. If it hangs, FFmpeg is not sending newlines or closing stdout.
                try:
                    line = self.process.stdout.readline()  # This is the primary suspect for hangs
                    if not line:  # Empty line can mean EOF if process also ended
                        if self.process.poll() is not None:
                            break  # Break if process ended
                        else:
                            time.sleep(0.05); continue  # Process alive, but no data yet, short sleep
                except Exception as e_readline:
                    self.log_message.emit(f"Error reading FFmpeg stdout line: {e_readline}")
                    break  # Exit progress loop on read error

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
                                break  # Break from progress loop
                        except ValueError:
                            self.log_message.emit(f"Warning: Could not parse progress: {line}")

            # After loop, ensure cancellation is handled
            if self._is_cancelled:
                if self.process and self.process.poll() is None:  # If still running
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                self.log_message.emit(f"Task {self.output_path.name} confirmed cancelled.")
                self.finished.emit(False, f"{str(self.output_path)} (Cancelled)")
                return

            # Process finished normally or broke from progress=end
            # Wait for full termination and get remaining output
            stdout_final, stderr_final = self.process.communicate(timeout=120)

            if self.process.returncode == 0: # SUCCESS
                self.log_message.emit(f"Successfully created: {str(self.output_path)}")
                if self.is_verbose_logging:
                    if stderr_final and stderr_final.strip(): # Only log if there's actual stderr content
                        self.log_message.emit(f"FFmpeg messages for {self.output_path.name} (verbose):\n{stderr_final.strip()}")
                # else: (if not verbose on success) do not log stderr_final
                self.finished.emit(True, str(self.output_path))
            else: # FAILURE
                self.log_message.emit(f"ERROR: FFmpeg command failed for {self.output_path} (exit code {self.process.returncode})")
                # Always log full stderr on actual FFmpeg error, regardless of verbose setting, as it's crucial for debugging.
                if stderr_final and stderr_final.strip():
                    self.log_message.emit(f"FFmpeg stderr:\n{stderr_final.strip()}")
                else:
                    self.log_message.emit("FFmpeg stderr: (No further error output from FFmpeg)") # If stderr was empty
                self.finished.emit(False, str(self.output_path))

        # ... (except FileNotFoundError, subprocess.TimeoutExpired, Exception as e) ...
        # Ensure these except blocks also emit self.finished(False, ...)

        finally:
            if self.process and self.process.poll() is None:
                self.log_message.emit(
                    f"Ensuring FFmpeg process termination for {self.output_path.name} (in finally)...")
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
            "6K": {"desc": "6k (6016xH)", "filter": "scale=6016:-2:flags=lanczos"},
            "4K": {"desc": "4k (3840xH)", "filter": "scale=3840:-2:flags=lanczos"},
            "1080p": {"desc": "1080p (1920xH)", "filter": "scale=1920:-2:flags=lanczos"},
            "720p": {"desc": "720p (1280xH)", "filter": "scale=1280:-2:flags=lanczos"},
            "custom": {"desc": "Custom WxH", "filter_template": "scale={w}:{h}:flags=lanczos"}
        }
        self.presets_dir = Path(".") / "timelapse_presets"  # Changed name
        self.presets_dir.mkdir(parents=True, exist_ok=True)
        self.settings = QSettings("My Timelapse App", "TimelapseMakerGUI")  # More specific org/app names
        self.current_theme = self.settings.value("theme", "light", type=str)  # Specify type for QSettings
        self.cpu_plot_widget = None
        self.cpu_plot_data_line = None
        self.log_text_edit = None
        self.theme_toggle_button = None # Ensure it's defined before apply_theme is called if init_ui is separate

        self.active_ffmpeg_worker = None # To keep track of the currently running FFmpeg task
        self.batch_cancelled_flag = False # Flag to stop processing further items in batch

        self.init_ui()

        self.ffmpeg_workers = []
        self.sequences_queue_for_batch = []
        self.current_batch_sequence_index = 0
        self.current_batch_sequence_generator = None
        self.processed_sequences_in_batch_count = 0  # For overall progress
        self.gpu_type_detected = None  # Store detected GPU type
        self.init_monitoring_data_and_start() # Initialize monitoring components

    def init_ui(self):
        main_layout = QVBoxLayout()

        # --- Top Bar with Theme Toggle ---
        top_bar_layout = QHBoxLayout();

        self.about_button = QPushButton("About") # Create About button
        self.about_button.setToolTip("Show application information") # Optional tooltip
        self.about_button.clicked.connect(self.show_about_dialog) # Connect to slot
        top_bar_layout.addWidget(self.about_button) # Add to left
        top_bar_layout.addStretch();
        self.theme_toggle_button = QPushButton("Switch to Dark Mode");
        self.theme_toggle_button.setCheckable(True);
        self.theme_toggle_button.clicked.connect(self.toggle_theme);
        top_bar_layout.addWidget(self.theme_toggle_button);
        main_layout.addLayout(top_bar_layout)

        # --- Directory Group ---
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

        # --- Filename Group ---
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

        # --- Core Settings Group ---
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

        # --- Encoding and Scaling Groups ---
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

        # --- Preset and Action Button Groups ---
        preset_layout = QHBoxLayout();
        self.load_preset_button = QPushButton("Load Preset");
        self.load_preset_button.clicked.connect(self.load_preset_action);
        self.save_preset_button = QPushButton("Save Settings as Preset");
        self.save_preset_button.clicked.connect(self.save_preset_action);
        preset_layout.addWidget(self.load_preset_button);
        preset_layout.addWidget(self.save_preset_button);
        main_layout.addLayout(preset_layout)
        action_and_cancel_layout = QHBoxLayout();
        self.scan_button = QPushButton("Scan for Sequences");
        self.scan_button.clicked.connect(self.scan_directories_action);
        self.scan_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed);
        action_and_cancel_layout.addWidget(self.scan_button);
        self.start_button = QPushButton("Start Batch Processing");
        self.start_button.setEnabled(False);
        self.start_button.clicked.connect(self.start_batch_action);
        self.start_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed);
        action_and_cancel_layout.addWidget(self.start_button);
        action_and_cancel_layout.addStretch();
        self.cancel_current_button = QPushButton("Cancel Current Sequence");
        self.cancel_current_button.clicked.connect(self.cancel_current_action);
        self.cancel_current_button.setEnabled(False);
        self.cancel_current_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed);
        action_and_cancel_layout.addWidget(self.cancel_current_button);
        self.cancel_batch_button = QPushButton("Cancel Entire Batch");
        self.cancel_batch_button.clicked.connect(self.cancel_batch_action);
        self.cancel_batch_button.setEnabled(False);
        self.cancel_batch_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed);
        action_and_cancel_layout.addWidget(self.cancel_batch_button);
        main_layout.addLayout(action_and_cancel_layout)

        # --- Verbose Log Checkbox ---
        log_options_layout = QHBoxLayout();
        self.verbose_log_checkbox = QCheckBox("Enable Verbose FFmpeg Logging");
        self.verbose_log_checkbox.setChecked(False);
        log_options_layout.addWidget(self.verbose_log_checkbox);
        log_options_layout.addStretch();
        main_layout.addLayout(log_options_layout)

        # --- Create a QSplitter for the main display areas ---
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Create a container for the tree and its label
        tree_container_widget = QWidget()
        tree_layout = QVBoxLayout(tree_container_widget)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.addWidget(QLabel("Found Sequence Directories / Sequences:"))
        self.dir_tree_widget = QTreeWidget()
        self.dir_tree_widget.setHeaderLabels(["Directory / Sequence", "Frames"])
        self.dir_tree_widget.itemChanged.connect(self.handle_tree_item_changed)
        # Make the first column (Directory/Sequence Name) stretch
        self.dir_tree_widget.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # Give the second column (Frames) a fixed or interactive size initially
        self.dir_tree_widget.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tree_layout.addWidget(self.dir_tree_widget)

        # Create a container for the log and its label
        log_container_widget = QWidget()
        log_layout = QVBoxLayout(log_container_widget)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(QLabel("Log:"))
        self.log_text_edit = QTextEdit()
        self.log_text_edit.setReadOnly(True)
        log_layout.addWidget(self.log_text_edit)

        # Set a minimum height for the log area
        tree_container_widget.setMinimumHeight(150)
        log_container_widget.setMinimumHeight(100)

        splitter.addWidget(tree_container_widget)
        splitter.addWidget(log_container_widget)
        splitter.setSizes([600, 300])

        # Add the splitter to the main layout
        main_layout.addWidget(splitter, 1)

        # --- Add Progress Bars AFTER the splitter ---
        self.current_sequence_progress_bar = QProgressBar();
        self.current_sequence_progress_bar.setTextVisible(True);
        main_layout.addWidget(QLabel("Current Sequence Progress:"));
        main_layout.addWidget(self.current_sequence_progress_bar)
        self.overall_batch_progress_bar = QProgressBar();
        self.overall_batch_progress_bar.setTextVisible(True);
        main_layout.addWidget(QLabel("Overall Batch Progress:"));
        main_layout.addWidget(self.overall_batch_progress_bar)

        # --- System Monitor Group ---
        monitor_group = QGroupBox("System Activity Monitor")
        monitor_layout = QVBoxLayout()
        # You need to import pyqtgraph as pg at the top of the file
        self.cpu_plot_widget = pg.PlotWidget(title="CPU Usage (%)")
        self.cpu_plot_widget.setYRange(0, 100, padding=0.05)
        self.cpu_plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.cpu_plot_widget.getPlotItem().hideAxis('bottom')
        self.cpu_plot_data_line = self.cpu_plot_widget.plot(pen='c', name="CPU")
        monitor_layout.addWidget(self.cpu_plot_widget)

        self.mem_plot_widget = pg.PlotWidget(title="Memory Usage (%)")
        self.mem_plot_widget.setYRange(0, 100, padding=0.05)
        self.mem_plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.mem_plot_widget.getPlotItem().hideAxis('bottom')
        self.mem_plot_data_line = self.mem_plot_widget.plot(pen='m', name="Memory")
        monitor_layout.addWidget(self.mem_plot_widget)

        self.gpu_plot_widget = pg.PlotWidget(title="GPU Usage (%) (If available)")
        self.gpu_plot_widget.setYRange(0, 100, padding=0.05)
        self.gpu_plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.gpu_plot_widget.getPlotItem().hideAxis('bottom')
        self.gpu_plot_data_line = self.gpu_plot_widget.plot(pen='y', name="GPU")
        self.gpu_plot_widget.setVisible(False)  # Hide initially
        monitor_layout.addWidget(self.gpu_plot_widget)

        monitor_group.setLayout(monitor_layout)
        main_layout.addWidget(monitor_group)

        self.setLayout(main_layout)

        # --- Final UI state updates ---
        self.update_scaling_options_ui()
        self.update_dynamic_codec_options_ui()
        self.apply_theme(self.current_theme)

    def show_about_dialog(self):
        # If using the custom AboutDialog:
        dialog = AboutDialog(self) # Pass parent
        dialog.exec() # Show as a modal dialog

    def handle_tree_item_changed(self, item: QTreeWidgetItem, column: int):
        if column == 0:  # Only act on changes in the first column (where checkboxes are)
            # Block signals to prevent recursive calls while we programmatically change states
            self.dir_tree_widget.blockSignals(True)

            current_check_state = item.checkState(0)

            # --- Case 1: A PARENT item's check state was changed by the user ---
            if item.parent() is None:  # This is a top-level (parent) item
                # If parent is checked or unchecked by user, propagate to all children
                if current_check_state == Qt.CheckState.Checked or current_check_state == Qt.CheckState.Unchecked:
                    for i in range(item.childCount()):
                        child = item.child(i)
                        if child.flags() & Qt.ItemFlag.ItemIsUserCheckable:  # Only if child is checkable
                            child.setCheckState(0, current_check_state)
                # Note: A parent item usually won't be set to PartiallyChecked by direct user click
                # if ItemIsAutoTristate is working correctly with its children.
                # If a parent IS partially checked, we don't automatically propagate that to children.

            # --- Case 2: A CHILD item's check state was changed by the user ---
            else:
                parent = item.parent()
                if parent:  # Should always have a parent if it's a child
                    num_children = parent.childCount()
                    checked_children_count = 0
                    unchecked_children_count = 0

                    for i in range(num_children):
                        child = parent.child(i)
                        if child.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                            if child.checkState(0) == Qt.CheckState.Checked:
                                checked_children_count += 1
                            elif child.checkState(0) == Qt.CheckState.Unchecked:
                                unchecked_children_count += 1

                    if checked_children_count == num_children:
                        # All children are checked, so parent becomes fully checked
                        parent.setCheckState(0, Qt.CheckState.Checked)
                    elif unchecked_children_count == num_children:
                        # All children are unchecked, so parent becomes fully unchecked
                        parent.setCheckState(0, Qt.CheckState.Unchecked)
                    else:
                        # Some are checked, some are not (or some are partially checked themselves if they were parents)
                        # So, parent becomes partially checked
                        parent.setCheckState(0, Qt.CheckState.PartiallyChecked)

            # Re-enable signals
            self.dir_tree_widget.blockSignals(False)

    def init_monitoring_data_and_start(self):  # RENAMED and MODIFIED
        self.cpu_data_history = [0] * 60  # Or a different history length
        self.mem_data_history = [0] * 60
        self.gpu_data_history = [0] * 60

        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self.update_monitors_display)

        self.check_gpu_availability_and_setup_plot()  # Sets up the GPU plot visibility

        self.monitor_timer.start(1000)  # START THE TIMER HERE (e.g., update every 1 second)
        self.log("System monitoring started.")

    # check_gpu_availability_and_setup_plot remains the same
    # update_monitors_display remains the same

    def check_gpu_availability_and_setup_plot(self): # Renamed and modified
        self.gpu_type_detected = monitoring_engine.detect_gpu_type() # Call engine
        if self.gpu_type_detected:
            self.gpu_plot_widget.setVisible(True)
            self.gpu_plot_widget.setTitle(f"{self.gpu_type_detected.upper()} GPU Usage (%)")
            self.log(f"{self.gpu_type_detected.upper()} GPU detected. Monitoring enabled.")
        else:
            self.gpu_plot_widget.setVisible(False)
            self.log("No common GPU monitoring tool found or supported for detailed stats by engine.")

    def update_monitors_display(self): # Renamed from update_monitors
        cpu_usage = monitoring_engine.get_cpu_usage()
        if cpu_usage is not None:
            self.cpu_data_history.pop(0); self.cpu_data_history.append(cpu_usage)
            self.cpu_plot_data_line.setData(self.cpu_data_history)

        mem_usage = monitoring_engine.get_memory_usage()
        if mem_usage is not None:
            self.mem_data_history.pop(0); self.mem_data_history.append(mem_usage)
            self.mem_plot_data_line.setData(self.mem_data_history)

        if self.gpu_type_detected:
            # get_gpu_usage now returns type and util, but we already have type
            _, gpu_util = monitoring_engine.get_gpu_usage() # Engine handles which specific util func to call
            if gpu_util is not None:
                self.gpu_data_history.pop(0); self.gpu_data_history.append(gpu_util)
            else: # Error or no data for this tick
                self.gpu_data_history.pop(0); self.gpu_data_history.append(0) # Append 0
            self.gpu_plot_data_line.setData(self.gpu_data_history)

    def cancel_current_action(self):
        if self.active_ffmpeg_worker and self.active_ffmpeg_worker.isRunning():
            self.log("Sending cancel signal to current FFmpeg task...")
            self.active_ffmpeg_worker.cancel_task()
        else:
            self.log("No FFmpeg task currently running to cancel.")

    def cancel_batch_action(self):  # <<< THIS IS THE METHOD
        self.log("Batch cancellation requested...")
        self.batch_cancelled_flag = True
        self.cancel_current_action()  # Attempt to cancel current task as well
        # UI updates to reflect cancellation state
        self.start_button.setEnabled(False)  # Can't restart a cancelled batch easily this way
        self.cancel_batch_button.setEnabled(False)  # Batch cancel is now in effect
        self.cancel_current_button.setEnabled(False)  # Current task is being cancelled
        self.log("Batch processing will stop. Any running sequence is being cancelled.")

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

    def scan_directories_action(self):
        self.log("Scanning for sequence directories...")
        parent_dir_ui = Path(self.parent_dir_edit.text())
        current_prefix = self.filename_prefix_edit.text() or ENGINE_DEFAULT_FILENAME_PREFIX
        current_suffix = self.filename_suffix_edit.text() or ENGINE_DEFAULT_FILENAME_SUFFIX

        self.dir_tree_widget.clear()
        self.dirs_to_process_cache = find_potential_sequence_dirs(parent_dir_ui, current_prefix, current_suffix)

        if not self.dirs_to_process_cache:
            self.log(
                f"No potential sequence subdirectories found in '{parent_dir_ui}' matching prefix='{current_prefix}', suffix='{current_suffix}'.")
            self.start_button.setEnabled(False)
            return

        self.log(
            f"Found {len(self.dirs_to_process_cache)} potential directory(s). Now scanning for sequences within them...")

        temp_settings_for_scan = self.gather_common_settings_from_ui()
        if not temp_settings_for_scan:
            self.log("Error: Could not gather current settings for sequence scanning. Using minimal defaults.")
            temp_settings_for_scan = {
                "main_output_dir": Path(self.output_dir_edit.text() or DEFAULT_OUTPUT_DIR),
                "filename_prefix_ui": current_prefix, "filename_suffix_ui": current_suffix,
                "output_extension": ".mp4", "video_codec": "libx264",
                "input_fps": DEFAULT_INPUT_FPS, "output_fps": DEFAULT_OUTPUT_FPS
            }

        total_sequences_added_to_tree = 0
        for parent_dir_path in self.dirs_to_process_cache:
            parent_item = QTreeWidgetItem(self.dir_tree_widget, [str(parent_dir_path.name)])
            parent_item.setData(0, Qt.ItemDataRole.UserRole, {"type": "parent_dir", "path": parent_dir_path})
            parent_item.setFlags(
                parent_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)  # Removed ItemIsAutoTristate for now
            parent_item.setCheckState(0, Qt.CheckState.Checked)  # <<<<<< CHANGED TO CHECKED BY DEFAULT

            sequences_found_in_this_parent = 0
            try:
                for _, output_p, frames in generate_ffmpeg_commands_for_sequences_in_dir(
                        parent_dir_path, current_prefix, current_suffix, temp_settings_for_scan):
                    seq_start_num_match = re.search(r"seq(\d+)", output_p.name)
                    seq_start_display = seq_start_num_match.group(1) if seq_start_num_match else "UnknownStart"

                    child_item = QTreeWidgetItem(parent_item,
                                                 [f"  Sequence starting ~{seq_start_display}", str(frames)])
                    child_item.setData(0, Qt.ItemDataRole.UserRole, {
                        "type": "sequence", "parent_path": parent_dir_path,
                        "start_number_str_approx": seq_start_display,
                        "num_frames_approx": frames
                    })
                    child_item.setFlags(child_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child_item.setCheckState(0, Qt.CheckState.Checked)  # <<<<<< CHANGED TO CHECKED BY DEFAULT
                    sequences_found_in_this_parent += 1
                    total_sequences_added_to_tree += 1
            except Exception as e:
                self.log(f"Error while scanning sequences in '{parent_dir_path.name}': {e}")
                error_child = QTreeWidgetItem(parent_item, [f"  (Error scanning: {e})", ""])
                error_child.setFlags(error_child.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)

            if sequences_found_in_this_parent == 0:
                no_seq_child = QTreeWidgetItem(parent_item, ["  (No sequences detected with current settings)", ""])
                no_seq_child.setFlags(no_seq_child.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                parent_item.setCheckState(0, Qt.CheckState.Unchecked)  # Uncheck parent if no children
                parent_item.setDisabled(True)  # Optionally disable parent if no sequences

            parent_item.setExpanded(True)

        if total_sequences_added_to_tree > 0:
            self.log(
                f"Scan complete. Displaying {total_sequences_added_to_tree} sequence(s) across {len(self.dirs_to_process_cache)} directory(s).")
            self.start_button.setEnabled(True)
        else:
            self.log("Scan complete. No processable sequences found with current settings.")
            self.start_button.setEnabled(False)

    def start_batch_action(self):
        self.log("Start batch action triggered.")
        self.sequences_queue_for_batch = []  # THIS WILL BE OUR QUEUE of sequence data dicts

        root = self.dir_tree_widget.invisibleRootItem()
        for i in range(root.childCount()):  # Iterate through parent directory items
            parent_item = root.child(i)
            # Iterate through child sequence items of this parent
            for j in range(parent_item.childCount()):
                child_item = parent_item.child(j)
                # We only care if the child (sequence) item itself is checked
                if child_item.checkState(0) == Qt.CheckState.Checked:
                    seq_data = child_item.data(0, Qt.ItemDataRole.UserRole)
                    # Ensure seq_data is the expected dictionary and type
                    if isinstance(seq_data, dict) and seq_data.get("type") == "sequence":
                        self.sequences_queue_for_batch.append(seq_data)

        if not self.sequences_queue_for_batch:
            self.log(
                "No sequences selected for processing. Please check the boxes next to the individual sequences you want to render.")
            return  # Correctly exits if nothing is selected

        self.log(f"Starting batch for {len(self.sequences_queue_for_batch)} selected sequence(s)...")
        self.start_button.setEnabled(False);
        self.scan_button.setEnabled(False)
        self.cancel_batch_button.setEnabled(True);
        self.cancel_current_button.setEnabled(False)
        self.batch_cancelled_flag = False

        self.common_settings_for_batch = self.gather_common_settings_from_ui()
        if not self.common_settings_for_batch:  # Checks if gather_settings returned None (or empty dict)
            self.log("Failed to gather valid settings. Aborting batch.")
            self.start_button.setEnabled(True);
            self.scan_button.setEnabled(True);  # Re-enable scan button too
            self.cancel_batch_button.setEnabled(False)  # Disable cancel if batch aborted
            return

            # Now we know the exact number of sequences
            self.current_batch_total_sequences = len(self.sequences_queue_for_batch)
            self.processed_sequences_in_batch_count = 0

            self.overall_batch_progress_bar.setMaximum(
                self.current_batch_total_sequences if self.current_batch_total_sequences > 0 else 1)  # Good check
            self.overall_batch_progress_bar.setValue(0)
            self.overall_batch_progress_bar.setFormat(
                f"Overall Sequences: %v/{self.current_batch_total_sequences if self.current_batch_total_sequences > 0 else 'N/A'}")

            # Start the monitor timer if needed
            if hasattr(self, 'monitor_timer') and not self.monitor_timer.isActive():
                self.monitor_timer.start(1000)
                self.log("System monitoring started for batch.")

            # Set the index to the first sequence and kick off the process
            self.current_batch_sequence_index = 0
            self.process_next_individual_sequence()  # Correctly calls the simplified processing method

    def process_next_individual_sequence(self):
            if self.batch_cancelled_flag:
                self.log("Batch was cancelled. Halting further processing.")
                self.cleanup_after_batch_or_cancel()
                return

            # Check if we have processed all items in our queue
            if self.current_batch_sequence_index >= len(self.sequences_queue_for_batch):
                self.log("===== Batch processing fully completed. =====")
                self.cleanup_after_batch_or_cancel()
                return

            # Get the next specific sequence from our queue
            sequence_to_process_data = self.sequences_queue_for_batch[self.current_batch_sequence_index]

            parent_dir = sequence_to_process_data["parent_path"]
            start_num = sequence_to_process_data["start_number_str"]  # This is just for logging/finding

            self.log(
                f"--- Preparing sequence starting ~{start_num} in {parent_dir.name} ({self.current_batch_sequence_index + 1}/{len(self.sequences_queue_for_batch)}) ---")

            # Now, call the engine generator for this parent directory, but find the specific sequence we want.
            # This is still inefficient, but it's the bridge to a better engine function later.
            prefix_to_use = self.common_settings_for_batch.get("filename_prefix_ui", ENGINE_DEFAULT_FILENAME_PREFIX)
            suffix_to_use = self.common_settings_for_batch.get("filename_suffix_ui", ENGINE_DEFAULT_FILENAME_SUFFIX)

            cmd_to_run, path_to_run, frames_to_run = None, None, None

            # Find the specific command for the sequence we're currently processing from the queue
            for ffmpeg_cmd, output_path, total_frames in generate_ffmpeg_commands_for_sequences_in_dir(
                    parent_dir, prefix_to_use, suffix_to_use, self.common_settings_for_batch):

                seq_start_num_match = re.search(r"seq(\d+)", output_path.name)
                if seq_start_num_match and seq_start_num_match.group(1) == sequence_data[
                    "start_number_str_approx"]:  # Use the stored approx
                    cmd_to_run, path_to_run, frames_to_run = ffmpeg_cmd, output_path, total_frames
                    break

            if cmd_to_run:
                self.current_sequence_progress_bar.setFormat(f"{path_to_run.name} - %p%")
                self.current_sequence_progress_bar.setValue(0)
                self.current_sequence_progress_bar.setMaximum(frames_to_run if frames_to_run > 0 else 100)

                is_verbose = self.verbose_log_checkbox.isChecked()
                self.active_ffmpeg_worker = FFmpegWorker(cmd_to_run, path_to_run, frames_to_run, is_verbose)
                self.active_ffmpeg_worker.progress_update.connect(self.update_current_sequence_progress_slot)
                self.active_ffmpeg_worker.log_message.connect(self.log)
                self.active_ffmpeg_worker.finished.connect(self.on_ffmpeg_worker_finished_slot)

                self.cancel_current_button.setEnabled(True)
                self.active_ffmpeg_worker.start()
            else:
                # This specific sequence was not found by the engine, maybe files were deleted since scanning.
                self.log(f"  Could not find/generate command for sequence starting ~{start_num}. Skipping.")
                # We must still call the 'finished' slot logic to move to the next item
                self.on_ffmpeg_worker_finished_slot(False, f"Sequence starting {start_num} not found by engine.")

    def on_ffmpeg_worker_finished_slot(self, success, output_file_str):
        self.active_ffmpeg_worker = None
        self.cancel_current_button.setEnabled(False)

        if success: self.log(f"  Sequence finished: {output_file_str}")
        else: self.log(f"  Sequence FAILED or CANCELLED: {output_file_str}")

        if not self.batch_cancelled_flag:
            self.processed_sequences_in_batch_count += 1
            if self.overall_batch_progress_bar.maximum() > 0:
                self.overall_batch_progress_bar.setValue(self.processed_sequences_in_batch_count)

        self.current_batch_sequence_index += 1 # IMPORTANT: Move to the next sequence in our queue
        self.process_next_individual_sequence() # Trigger processing for the next item

    def cleanup_after_batch_or_cancel(self):
        """Resets UI elements after batch completion or cancellation."""
        # if self.monitor_timer.isActive():
        #   self.monitor_timer.stop()
        self.scan_button.setEnabled(True)
        self.start_button.setEnabled(True if self.dir_tree_widget.topLevelItemCount() > 0 else False)
        self.cancel_batch_button.setEnabled(False)
        self.cancel_current_button.setEnabled(False)
        if self.batch_cancelled_flag :
            self.overall_batch_progress_bar.setFormat("Batch Cancelled")
        else:
            self.overall_batch_progress_bar.setFormat("Batch Complete!")
            if self.overall_batch_progress_bar.maximum() > 0 : # Ensure it shows 100% if tasks ran
                 self.overall_batch_progress_bar.setValue(self.overall_batch_progress_bar.maximum())
        self.active_ffmpeg_worker = None # Ensure cleared
        self.batch_cancelled_flag = False # Reset for next run

    def update_current_sequence_progress_slot(self, current_frame, total_frames):
        if total_frames > 0: # Check total_frames before setting maximum
            if self.current_sequence_progress_bar.maximum() != total_frames:
                 self.current_sequence_progress_bar.setMaximum(total_frames)
            self.current_sequence_progress_bar.setValue(current_frame)
        else: # Handle case where total_frames might be 0 (e.g., error or empty sequence)
            self.current_sequence_progress_bar.setMaximum(100) # Default max
            self.current_sequence_progress_bar.setValue(0)

    def closeEvent(self, event):
        """Ensure timer is stopped when the application window is closed."""
        self.log("Application closing, stopping monitor timer...")
        if hasattr(self, 'monitor_timer') and self.monitor_timer.isActive():
            self.monitor_timer.stop()
        # Clean up any running FFmpeg workers if necessary
        if self.active_ffmpeg_worker and self.active_ffmpeg_worker.isRunning():
            self.log("Stopping active FFmpeg worker...")
            self.active_ffmpeg_worker.cancel_task() # Ask it to terminate
            if not self.active_ffmpeg_worker.wait(3000): # Wait up to 3s
                 self.log("FFmpeg worker did not terminate gracefully on close.")
                 # It should be killed by its own finally block or OS
        super().closeEvent(event) # Important to call the base class method

class AboutDialog(QDialog):  # Import QDialog from PyQt6.QtWidgets
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Timelapse Maker")

        layout = QVBoxLayout(self)

        # --- Icon ---
        icon_label = QLabel()
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)  # Good to keep for centering

        icon_path = Path(__file__).resolve().parent / "icon.png"
        if icon_path.exists():
            pixmap = QPixmap(str(icon_path))
            if not pixmap.isNull():  # Good practice to check if pixmap loaded correctly
                icon_label.setPixmap(
                    pixmap.scaled(120, 120,
                                  Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
                )
            else:
                icon_label.setText("(Icon Load Error)")  # Fallback if file exists but can't be loaded
                print(f"Warning: Could not load icon from {icon_path}")  # Log the error
        else:
            icon_label.setText("(Icon Not Found)")  # Fallback if file doesn't exist
            print(f"Warning: Icon file not found at {icon_path}")  # Log the error

        layout.addWidget(icon_label)  # <<<< ENSURE THIS IS CALLED

        # --- Title ---
        title_label = QLabel("Timelapse Maker GUI")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("font-size: 16pt; font-weight: bold;")
        layout.addWidget(title_label)

        # --- Version ---
        version_label = QLabel("Version: 0.4.2 (Python Edition)")  # Update as needed
        version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version_label)

        # --- Description ---
        description_text = (
            "This application helps create timelapse videos from sequences of images "
            "using FFmpeg.\n\n"
            "Developed with Python and PyQt6.\n"
            "Inspired by your great ideas!"
        )
        desc_label = QLabel(description_text)
        desc_label.setWordWrap(True)
        desc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc_label)

        layout.addStretch()

        # --- OK Button ---
        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self.accept)  # Closes the dialog
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(ok_button)
        button_layout.addStretch()
        layout.addLayout(button_layout)

        self.setMinimumWidth(350)
        self.setLayout(layout)

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