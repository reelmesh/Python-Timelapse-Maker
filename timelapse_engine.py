# timelapse_engine.py
import os
import re
from pathlib import Path
import subprocess # For ffprobe
import shlex # For joining command for display if needed by engine

# --- Engine Default Configurations ---
# These are defaults for the core processing logic.
# The GUI might have its own defaults for UI elements that override these or provide choices.
ENGINE_DEFAULT_FILENAME_PREFIX = "P"
ENGINE_DEFAULT_FILENAME_SUFFIX = ".JPG"
# Add other engine-specific defaults if any (e.g., a fallback pixel format if not specified)

# --- Helper Functions ---
def get_numeric_part(filename_str: str, prefix: str, suffix: str) -> str | None:
    """
    Extracts the numeric string between a prefix and suffix.
    Returns the numeric string (e.g., "0001") or None if no match.
    """
    if filename_str.startswith(prefix) and filename_str.endswith(suffix):
        potential_num_part = filename_str[len(prefix):-len(suffix)]
        if potential_num_part.isdigit():
            return potential_num_part
    return None

def get_image_dimensions(image_path: Path) -> tuple[int, int] | None:
    """Gets width and height of an image using ffprobe."""
    try:
        ffprobe_cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'csv=s=x:p=0',
            str(image_path)
        ]
        result = subprocess.run(ffprobe_cmd, capture_output=True, text=True, check=True)
        width_str, height_str = result.stdout.strip().split('x')
        return int(width_str), int(height_str)
    except Exception as e:
        print(f"Engine Warning: Could not get dimensions for {image_path}: {e}")
        return None

# --- Core Logic Functions ---
def find_potential_sequence_dirs(parent_dir_path: Path, filename_prefix: str, filename_suffix: str) -> list[Path]:
    parent_dir_path = Path(parent_dir_path)
    found_dirs = []
    if not parent_dir_path.is_dir():
        print(f"Engine Error: Parent directory '{parent_dir_path}' not found.")
        return found_dirs

    print(f"Engine: Scanning '{parent_dir_path}' for subdirectories...") # More specific
    item_count = 0
    for item in parent_dir_path.iterdir():
        item_count += 1
        print(f"  Engine: Checking item: {item}, IsDir: {item.is_dir()}") # ADD THIS
        if item.is_dir():
            files_in_subdir_count = 0
            has_matching_file = False
            for f in item.iterdir():
                if f.is_file():
                    files_in_subdir_count +=1
                    # print(f"    Engine: Checking file in subdir: {f.name}") # Can be very verbose
                    numeric_part = get_numeric_part(f.name, filename_prefix, filename_suffix)
                    if numeric_part is not None:
                        print(f"    Engine: Found matching file in {item.name}: {f.name}") # ADD THIS
                        has_matching_file = True
                        break # Found one, no need to check further in this subdir for *this* purpose
            # print(f"  Engine: Subdir {item.name} has {files_in_subdir_count} files. Matching file found: {has_matching_file}") # ADD THIS
            if has_matching_file:
                print(f"  Engine: Adding potential sequence directory: {item}") # ADD THIS
                found_dirs.append(item)
    if item_count == 0:
        print(f"Engine: No items (files or subdirs) found in '{parent_dir_path}'.")
    return found_dirs

def count_total_sequences_in_paths(
        parent_dir_paths: list[Path],
        filename_prefix: str,
        filename_suffix: str
) -> int:
    """
    Counts the total number of distinct image sequences across multiple parent directories.
    This is a "dry run" version of the sequence detection.
    """
    total_sequence_count = 0
    for directory_path in parent_dir_paths:
        if not directory_path.is_dir():
            continue

        all_potential_files_info = []
        for file_path in directory_path.iterdir():
            if file_path.is_file():
                num_str = get_numeric_part(file_path.name, filename_prefix, filename_suffix)
                if num_str:
                    try:
                        all_potential_files_info.append((file_path, num_str, int(num_str)))
                    except ValueError:
                        continue

        if not all_potential_files_info: continue
        all_potential_files_info.sort(key=lambda x: (x[2], x[0].name))

        processed_files_in_this_dir = set()
        current_dir_sequence_count = 0

        for start_file_path, start_num_str_from_scan, start_num_val in all_potential_files_info:
            if start_file_path in processed_files_in_this_dir:
                continue

            current_dir_sequence_count += 1
            # Simplified counting: just find the start and mark based on that for this count
            # A more accurate count would do the full contiguous check, but this is faster for total estimation.
            # For full accuracy, we'd replicate the inner while loop of generate_ffmpeg_commands...
            # For now, assume each distinct start_file_path found this way is one sequence.
            # To be more robust for counting, we should at least mark the first file.
            # A truly robust count would do the inner contiguous counting.
            # Let's do a slightly more robust count:

            num_digits_for_pattern = len(start_num_str_from_scan)
            image_pattern_basename_for_count = f"{filename_prefix}%0{num_digits_for_pattern}d{filename_suffix}"

            temp_current_num_val = start_num_val
            sequence_had_frames = False
            while True:
                expected_filename = image_pattern_basename_for_count % temp_current_num_val
                expected_file_path = directory_path / expected_filename
                if expected_file_path.is_file():
                    processed_files_in_this_dir.add(expected_file_path)  # Mark as processed for *this dir's count*
                    sequence_had_frames = True
                    temp_current_num_val += 1
                else:
                    break
            if not sequence_had_frames:  # If the first file itself didn't start a sequence
                current_dir_sequence_count -= 1

        total_sequence_count += current_dir_sequence_count
    return total_sequence_count


# Modify generate_ffmpeg_commands_for_sequences_in_dir for the new output_basename_ui
def generate_ffmpeg_commands_for_sequences_in_dir(
        directory_path: Path,
        filename_prefix: str,
        filename_suffix: str,
        common_settings: dict
):
    # ... (Existing logic at the start: find all_potential_files_info, sort, etc. - keep as is) ...
    directory_path = Path(directory_path)
    all_potential_files_info = []
    for file_path in directory_path.iterdir():
        if file_path.is_file():
            num_str = get_numeric_part(file_path.name, filename_prefix, filename_suffix)
            if num_str:
                try:
                    all_potential_files_info.append((file_path, num_str, int(num_str)))
                except ValueError:
                    continue
    if not all_potential_files_info: return
    all_potential_files_info.sort(key=lambda x: (x[2], x[0].name))
    processed_files_in_this_dir = set()
    sequences_found_count = 0

    for start_file_path, start_num_str_from_scan, start_num_val in all_potential_files_info:
        if start_file_path in processed_files_in_this_dir: continue
        sequences_found_count += 1
        num_digits_for_pattern = len(start_num_str_from_scan)
        image_pattern_basename_for_ffmpeg = f"{filename_prefix}%0{num_digits_for_pattern}d{filename_suffix}"
        frames_in_this_sequence_paths = []
        temp_current_num_val = start_num_val
        while True:
            expected_filename = image_pattern_basename_for_ffmpeg % temp_current_num_val
            expected_file_path = directory_path / expected_filename
            if expected_file_path.is_file():
                frames_in_this_sequence_paths.append(expected_file_path)
                processed_files_in_this_dir.add(expected_file_path)
                temp_current_num_val += 1
            else:
                break

        if frames_in_this_sequence_paths:
            num_frames_to_process = len(frames_in_this_sequence_paths)
            actual_ffmpeg_start_number_str = get_numeric_part(frames_in_this_sequence_paths[0].name, filename_prefix,
                                                              filename_suffix)
            first_image_path_of_sequence = frames_in_this_sequence_paths[0]

            # --- HW Accel Scaling Logic (Keep your existing logic here) ---
            img_width, img_height = 0, 0;
            dimensions = get_image_dimensions(first_image_path_of_sequence)
            if dimensions:
                img_width, img_height = dimensions
            else:
                print(f"  Engine Warning: Could not get dimensions for {first_image_path_of_sequence.name}.")
            current_scale_filter_from_ui = common_settings.get("scale_filter_string", "");
            current_resolution_desc_from_ui = common_settings.get("resolution_desc", "Original");
            video_codec = common_settings.get("video_codec", "libx264")
            MAX_NVENC_H264_WIDTH = 4096;
            MAX_NVENC_HEVC_WIDTH = 8192;
            needs_hw_scaling_adjustment = False;
            target_hw_scale_width = 0
            if img_width > 0 and not current_scale_filter_from_ui:
                if video_codec == "h264_nvenc" and img_width > MAX_NVENC_H264_WIDTH:
                    needs_hw_scaling_adjustment = True; target_hw_scale_width = MAX_NVENC_H264_WIDTH
                elif video_codec == "hevc_nvenc" and img_width > MAX_NVENC_H264_WIDTH:
                    needs_hw_scaling_adjustment = True; target_hw_scale_width = MAX_NVENC_H264_WIDTH  # Simplified
            effective_scale_filter = current_scale_filter_from_ui;
            effective_resolution_desc = current_resolution_desc_from_ui
            if needs_hw_scaling_adjustment:
                effective_scale_filter = f"scale={target_hw_scale_width}:-2:flags=lanczos";
                effective_resolution_desc = f"AutoScaled-{target_hw_scale_width}w"
            # --- End HW Accel Scaling Logic ---

            # --- Construct Filename ---
            # Use user-defined base name if provided, else use directory name
            user_defined_basename = common_settings.get("output_basename_ui", "").strip()
            file_base = user_defined_basename if user_defined_basename else directory_path.name

            seq_tag = f"seq{actual_ffmpeg_start_number_str}"
            codec_for_fn = video_codec.replace("_nvenc", "Nvenc").replace("_qsv", "QSV").replace("_amf", "AMF")

            output_filename_parts = [file_base, seq_tag, codec_for_fn]  # Use file_base
            # ... (Rest of your existing output_filename_parts construction) ...
            if common_settings.get("prores_profile_val") is not None and video_codec == "prores_ks":
                for pk, pv in common_settings.get("prores_profiles_map", {}).items():
                    if pv == common_settings["prores_profile_val"]: output_filename_parts.append(
                        f"p{pk.lower()}"); break
            if common_settings.get("dnx_bitrate_or_profile") and video_codec == "dnxhd":
                output_filename_parts.append(
                    common_settings["dnx_bitrate_or_profile"].replace("dnxhr_", "").replace("M", "").replace("K", ""))
            output_filename_parts.append(
                f"{common_settings.get('input_fps', 10.0)}in-{common_settings.get('output_fps', 30.0)}out")
            if common_settings.get("hw_cq_value") is not None and common_settings.get("hwaccel_type", "none") != "none":
                output_filename_parts.append(f"cq{common_settings['hw_cq_value']}")
            elif common_settings.get("is_crf_based") and common_settings.get("crf_value") is not None:
                output_filename_parts.append(f"crf{common_settings['crf_value']}")
            if common_settings.get("hw_preset"):
                output_filename_parts.append(common_settings['hw_preset'])
            elif common_settings.get("codec_preset"):
                if video_codec == "libvpx-vp9":
                    output_filename_parts.append(f"dl{common_settings['codec_preset']}")
                    if common_settings.get("vp9_cpu_used") is not None: output_filename_parts.append(
                        f"cpu{common_settings['vp9_cpu_used']}")
                elif video_codec in ["libx264", "libx265"]:
                    output_filename_parts.append(common_settings['codec_preset'])

            if effective_scale_filter:
                res_tag_fn = effective_resolution_desc.split('(')[0].strip().replace(' ', '_').replace('%_of_original',
                                                                                                       '%orig').lower()
                output_filename_parts.append(res_tag_fn)
            else:
                output_filename_parts.append("orig")

            output_video_filename = "_".join(str(p) for p in output_filename_parts if p) + common_settings.get(
                "output_extension", ".mp4")
            final_output_path = common_settings.get("main_output_dir", Path(".")) / output_video_filename

            # ... (Rest of your FFmpeg command construction using effective_scale_filter etc. - keep as is) ...
            ffmpeg_cmd = ['ffmpeg', '-y', '-framerate', str(common_settings.get('input_fps', 10.0)), '-start_number',
                          actual_ffmpeg_start_number_str, '-i', str(directory_path / image_pattern_basename_for_ffmpeg),
                          '-vframes', str(num_frames_to_process)]
            final_vf_string = ""
            if effective_scale_filter: final_vf_string = effective_scale_filter
            if common_settings.get("pixel_format_final"):
                if final_vf_string:
                    final_vf_string += f",format={common_settings['pixel_format_final']}"
                else:
                    final_vf_string = f"format={common_settings['pixel_format_final']}"
            if final_vf_string: ffmpeg_cmd.extend(['-vf', final_vf_string])
            ffmpeg_cmd.extend(['-c:v', video_codec])
            is_hw_encoder_active = (
                        common_settings.get("hwaccel_type", "none") != "none" and video_codec != common_settings.get(
                    "base_codec"))
            if is_hw_encoder_active:
                if common_settings.get("hw_cq_value") is not None:
                    hw_type = common_settings.get("hwaccel_type");
                    cq_value = str(common_settings['hw_cq_value'])
                    if hw_type == "nvenc":
                        ffmpeg_cmd.extend(['-cq', cq_value])
                    elif hw_type == "qsv":
                        ffmpeg_cmd.extend(['-global_quality', cq_value])
                    elif hw_type == "amf":
                        print(
                            f"Engine Note: AMF CQ/QP for {final_output_path} may need specific flags for CQ {cq_value}.")
                if common_settings.get("hw_preset"):
                    hw_type = common_settings.get("hwaccel_type");
                    hw_preset_val = common_settings['hw_preset']
                    if hw_type == "nvenc":
                        ffmpeg_cmd.extend(['-preset:v', hw_preset_val])
                    elif hw_type == "amf":
                        ffmpeg_cmd.extend(['-quality', hw_preset_val])
                    elif hw_type == "qsv" and hw_preset_val:
                        print(f"Engine Note: QSV preset '{hw_preset_val}' for {final_output_path} selected.")
            else:  # Software params
                if common_settings.get("codec_preset"):
                    if video_codec == "libvpx-vp9":
                        ffmpeg_cmd.extend(['-deadline', common_settings['codec_preset']])
                    elif video_codec in ["libx264", "libx265"]:
                        ffmpeg_cmd.extend(['-preset', common_settings['codec_preset']])
                if common_settings.get("is_crf_based") and common_settings.get("crf_value") is not None:
                    ffmpeg_cmd.extend(['-crf', str(common_settings['crf_value'])])
                    if video_codec == "libvpx-vp9": ffmpeg_cmd.extend(['-b:v', '0'])
                if common_settings.get(
                    "prores_profile_val") is not None and video_codec == "prores_ks": ffmpeg_cmd.extend(
                    ['-profile:v', str(common_settings['prores_profile_val'])])
                if common_settings.get("dnx_bitrate_or_profile") and video_codec == "dnxhd":
                    dnx_val = common_settings.get("dnx_bitrate_or_profile", "")
                    if dnx_val.lower().endswith(('m', 'k')):
                        ffmpeg_cmd.extend(['-b:v', dnx_val])
                    else:
                        ffmpeg_cmd.extend(['-profile:v', dnx_val])
                if common_settings.get("vp9_cpu_used") is not None and video_codec == "libvpx-vp9": ffmpeg_cmd.extend(
                    ['-cpu-used', str(common_settings['vp9_cpu_used'])])
            ffmpeg_cmd.extend(['-r', str(common_settings.get('output_fps', 30.0)), '-pix_fmt',
                               common_settings.get('pixel_format_final', 'yuv420p'), str(final_output_path)])

            yield ffmpeg_cmd, final_output_path, num_frames_to_process

    # if sequences_found_count == 0: # No need to print from engine, GUI can handle it
    #     pass