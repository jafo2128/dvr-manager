#!/usr/bin/env python3

import cv2
import os
import re
import PySimpleGUI as sg
import sqlite3
import subprocess
import sys

from datetime import datetime
from enum     import Enum
from typing   import cast, Callable, Iterator, Optional, Tuple

# Enigma 2 video file extension (default: ".ts")
E2_VIDEO_EXTENSION = ".ts"
# Enigma 2 meta file extension (default: ".ts.meta")
E2_META_EXTENSION = ".ts.meta"
# As far as I know there are six files associated to each recording
E2_EXTENSIONS = [".eit", ".ts", ".ts.ap", ".ts.cuts", ".ts.meta", ".ts.sc"]

# A file to which the dropped file paths are appended
DROPPED_FILE = "dropped"

# The default GUI font
GUI_FONT = ("JetBrains Mono", 14)

class QueryType(Enum):
    ATTRIBUTE = 0
    AGGREGATE = 1

class SortOrder(Enum):
    ASC = 0
    DESC = 1

    def __str__(self) -> str:
        return super().__str__().strip(f"{self.__class__.__name__}.")

class Recording:
    basepath: str
    file_basename: str
    file_size: int
    epg_channel: str
    epg_title: str
    epg_description: str
    video_duration: int
    video_height: int
    video_width: int
    video_fps: int
    is_good: bool
    is_dropped: bool
    is_mastered: bool
    groupkey: str
    sortkey: int
    comment: str
    timestamp: str

    def __getattributes(rec) -> str:
        return f"{'D' if rec.is_dropped else '.'}{'G' if rec.is_good else '.'}{'M' if rec.is_mastered else '.'}{'C' if len(rec.comment) > 0 else '.'}"

    def __repr__(rec) -> str:
        return f"{rec.__getattributes()} | {rec.timestamp} | {(to_GiB(rec.file_size)):4.1f} GiB | {(rec.video_duration // 60):3d} min | {rec.epg_channel[:10].ljust(10)} | {rec.epg_title[:42].ljust(42)} | {rec.epg_description}"

# Recording objects
recordings: list[Recording] = []
# PySimpleGUI window object
window: sg.Window
# Recording cache database
database = sqlite3.connect("recordings.sqlite3")

class RecordingFactory:
    @staticmethod
    def from_meta_file(basepath: str, meta: list[str]) -> Recording:
        rec = Recording()

        rec.basepath = basepath

        rec.file_basename, rec.file_size = os.path.basename(basepath), os.stat(basepath + E2_VIDEO_EXTENSION).st_size
        rec.epg_channel, rec.epg_title = meta[0].split(":")[-1].strip(), meta[1].strip()
        rec.epg_description = remove_prefix(meta[2].strip(), rec.epg_title).strip()
        rec.video_duration, rec.video_height, rec.video_width, rec.video_fps = get_video_metadata(rec)
        rec.is_good, rec.is_dropped, rec.is_mastered = False, False, False
        rec.groupkey  = make_groupkey(rec.epg_title)
        rec.comment = ""

        basename_tokens = rec.file_basename.split(" - ")

        rec.timestamp = datetime.strftime(
            datetime.strptime(basename_tokens[0], "%Y%m%d %H%M"),
            "%Y-%m-%d %H:%M")

        if len(rec.epg_channel) == 0:
            rec.epg_channel = basename_tokens[1]
        if len(rec.epg_title) == 0:
            rec.epg_title = basename_tokens[2]

        return rec

    @staticmethod
    def from_database(basepath: str) -> Optional[Recording]:
        basename = os.path.basename(basepath)
        rec = db_load(basename)
        if rec is None:
            return None

        assert rec.file_size == os.stat(basepath + E2_VIDEO_EXTENSION).st_size

        rec.basepath = basepath

        return rec

# Remove everything that is not a letter or digit
def make_groupkey(line: str) -> str:
    # Add some more translations if desired
    translations = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss"
    }

    return re.sub("[^a-z0-9]+", "",
                  line.lower()
                      .translate(str.maketrans(translations)))

def remove_prefix(line: str, prefix: str) -> str:
    return re.sub(f"^{re.escape(prefix)}", "", line)

def to_GiB(size: int) -> float:
    return size / 1_073_741_824

def drop_recording(rec: Recording) -> None:
    with open(DROPPED_FILE, "a") as f:
        for e in E2_EXTENSIONS:
            filepath = rec.basepath + e
            if os.path.exists(filepath):
                print(filepath, file=f)
    db_remove(rec)

def sort_recordings(order_by: str, query_type: QueryType, sort_order: SortOrder) -> None:
    key_ranks = db_rank(order_by, query_type, sort_order)
    if query_type == QueryType.ATTRIBUTE:
        for r in recordings:
            r.sortkey = key_ranks.get(r.file_basename, 0)
    if query_type == QueryType.AGGREGATE:
        for r in recordings:
            r.sortkey = key_ranks.get(r.groupkey, 0)
    recordings.sort(key=lambda r: r.sortkey)

def update_attribute(recs: list[Recording],
                     check: Callable[[Recording], bool],
                     update: Callable[[Recording], None]) -> None:
    if len(recs) == 0:
        return
    for r in recs:
        if check(r):
            update(r)
            db_save(r)
            i = recordings.index(r)
            window["recordingBox"].widget.delete(i)
            window["recordingBox"].widget.insert(i, r)
    gui_reselect(recs)

def get_video_metadata(rec: Recording) -> Tuple[int, int, int, int]:
    vid = cv2.VideoCapture(rec.basepath + E2_VIDEO_EXTENSION)

    fps    = int(vid.get(cv2.CAP_PROP_FPS))
    frames = int(vid.get(cv2.CAP_PROP_FRAME_COUNT))
    height = int(vid.get(cv2.CAP_PROP_FRAME_HEIGHT))
    width  = int(vid.get(cv2.CAP_PROP_FRAME_WIDTH))

    vid.release()

    duration = frames // fps if fps != 0 else -1

    return (duration, height, width, fps)

def gui_init() -> None:
    sg.ChangeLookAndFeel("Dark Black")

    gui_layout = [[sg.Column([[sg.Text(key="informationTxt",
                               font=GUI_FONT)],
                              [sg.HorizontalSeparator(color="green")],
                              [sg.Text("[O]pen in VLC | [C]omment | [D]rop | [G]ood | [M]astered | Undo: [Shift + 'Key']",
                               font=GUI_FONT, text_color="grey")],
                              [sg.HorizontalSeparator(color="green")],
                              [sg.Text("Order by", font=GUI_FONT, text_color="grey"), sg.Column([
                              [sg.Radio("Title", "sortRadio", font=GUI_FONT, enable_events=True, default=True, metadata=("groupkey", QueryType.ATTRIBUTE)),
                               sg.Radio("Channel", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("epg_channel", QueryType.ATTRIBUTE)),
                               sg.Radio("Date", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("timestamp", QueryType.ATTRIBUTE)),
                               sg.Radio("Size", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("file_size", QueryType.ATTRIBUTE)),
                               sg.Radio("Length", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("video_duration", QueryType.ATTRIBUTE)),
                               sg.Radio("drop", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("is_dropped", QueryType.ATTRIBUTE)),
                               sg.Radio("good", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("is_good", QueryType.ATTRIBUTE)),
                               sg.Radio("mastered", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("is_mastered", QueryType.ATTRIBUTE)),
                               sg.Radio("COUNT", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("COUNT(*)", QueryType.AGGREGATE)),],
                              [sg.Radio("AVG(size)", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("AVG(file_size)", QueryType.AGGREGATE)),
                               sg.Radio("MAX(size)", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("MAX(file_size)", QueryType.AGGREGATE)),
                               sg.Radio("SUM(size)", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("SUM(file_size)", QueryType.AGGREGATE)),
                               sg.Radio("ANY(drop)", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("MAX(is_dropped)", QueryType.AGGREGATE)),
                               sg.Radio("ANY(good)", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("MAX(is_good)", QueryType.AGGREGATE)),
                               sg.Radio("ANY(mastered)", "sortRadio", font=GUI_FONT, enable_events=True, metadata=("MAX(is_mastered)", QueryType.AGGREGATE)),]]),
                               sg.Push(), sg.VerticalSeparator(color="green"), sg.Column([
                              [sg.Radio("ASC", "orderRadio", font=GUI_FONT, enable_events=True, default=True, metadata=SortOrder.ASC)],
                              [sg.Radio("DESC", "orderRadio", font=GUI_FONT, enable_events=True, metadata=SortOrder.DESC)]])],
                              [sg.HorizontalSeparator(color="green")],
                              [sg.Text("SELECT Mode", key="metaTxt", font=GUI_FONT, text_color="yellow"),
                               sg.VerticalSeparator(color="green"),
                               sg.Text(key="selectionTxt", font=GUI_FONT, text_color="yellow"),
                               sg.Push(), sg.Button("Drop", key="dropBtn")],]),
                   sg.Push(),
                   sg.Multiline(key="commentMul",
                                size=(80, 6),
                                font=GUI_FONT,
                                disabled=True)],
                  [sg.Listbox(key="recordingBox",
                              values=recordings,
                              size=(1280, 720),
                              enable_events=True,
                              font=GUI_FONT,
                              select_mode=sg.LISTBOX_SELECT_MODE_EXTENDED)]]

    global window
    window = sg.Window(title="DVR Duplicate Removal Tool",
                       layout=gui_layout,
                       return_keyboard_events=True,
                       resizable=True,
                       finalize=True)

    window["recordingBox"].set_focus()
    window["recordingBox"].widget.config(fg="white", bg="black")
    window["commentMul"].widget.config(fg="white", bg="black")

def gui_recolor(window: sg.Window) -> None:
    for i, r in enumerate(recordings):
        if r.is_dropped:
            window["recordingBox"].widget.itemconfig(i, fg="white", bg="red")
            continue

        if r.is_mastered:
            window["recordingBox"].widget.itemconfig(i, fg="white", bg="blue")
            continue

        if r.is_good:
            window["recordingBox"].widget.itemconfig(i, fg="black", bg="light green")
            continue

        window["recordingBox"].widget.itemconfig(i, fg="white", bg="black")

def gui_reselect(recs: list[Recording]) -> None:
    jump_indices = [i for i, r in enumerate(recordings) if r in recs]
    for i in jump_indices:
        window["recordingBox"].widget.selection_set(i)
    window["recordingBox"].widget.see(jump_indices[0])

def db_init() -> None:
    c = database.cursor()
    c.execute("""
              CREATE TABLE IF NOT EXISTS
                recordings(file_basename VARCHAR PRIMARY KEY, groupkey VARCHAR,
                  timestamp DATETIME, file_size INT,
                  epg_channel VARCHAR, epg_title VARCHAR, epg_description VARCHAR,
                  video_duration INT, video_height INT, video_width INT, video_fps INT,
                  is_good BOOL, is_dropped BOOL, is_mastered BOOL, comment VARCHAR);
              """)

def db_load(basename: str) -> Optional[Recording]:
    c = database.cursor()
    c.execute("""
              SELECT file_basename, file_size,
                epg_channel, epg_title, epg_description,
                video_duration, video_height, video_width, video_fps,
                is_good, is_dropped, is_mastered, groupkey, comment, timestamp
              FROM recordings
              WHERE file_basename = ?;
              """, (basename, ))
    raw = c.fetchone()

    if raw is None:
        return None

    rec = Recording()
    rec.file_basename, rec.file_size = raw[0], int(raw[1])
    rec.epg_channel, rec.epg_title, rec.epg_description = raw[2], raw[3], raw[4]
    rec.video_duration, rec.video_height, rec.video_width, rec.video_fps = raw[5], raw[6], raw[7], raw[8]
    rec.is_good, rec.is_dropped, rec.is_mastered = bool(raw[9]), raw[10], bool(raw[11])
    rec.groupkey, rec.comment = raw[12], raw[13]
    rec.timestamp = raw[14]

    return rec

def db_rank(order_by: str, query_type: QueryType, sort_order: SortOrder) -> dict[str, int]:
    # Yes, the following database calls are vulnerable to SQL injections,
    # but the tuple solution does not work here.
    # Please let me know if you have a better solution...

    c = database.cursor()
    if query_type == QueryType.ATTRIBUTE:
        c.execute(f"""
                  SELECT file_basename,
                         ROW_NUMBER() OVER (ORDER BY {order_by} {sort_order}, groupkey, timestamp)
                  FROM recordings
                  ORDER BY file_basename;
                  """)
    if query_type == QueryType.AGGREGATE:
        c.execute(f"""
                  SELECT groupkey,
                         ROW_NUMBER() OVER (ORDER BY {order_by} {sort_order}, groupkey, timestamp)
                  FROM recordings
                  GROUP BY groupkey
                  ORDER BY groupkey;
                  """)

    return dict(c.fetchall())

def db_save(rec: Recording) -> None:
    db_remove(rec)
    c = database.cursor()
    c.execute("""
              INSERT INTO recordings(file_basename, file_size,
                epg_channel, epg_title, epg_description,
                video_duration, video_height, video_width, video_fps,
                is_good, is_dropped, is_mastered, groupkey,
                comment, timestamp)
              VALUES (?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?);
              """, (rec.file_basename, rec.file_size,
              rec.epg_channel, rec.epg_title, rec.epg_description,
              rec.video_duration, rec.video_height, rec.video_width, rec.video_fps,
              rec.is_good, rec.is_dropped, rec.is_mastered, rec.groupkey,
              rec.comment, rec.timestamp))

    database.commit()

def db_remove(rec: Recording) -> None:
    c = database.cursor()
    c.execute("""
              DELETE FROM recordings
              WHERE file_basename = ?
              """, (rec.file_basename, ))

    assert c.rowcount <= 1
    database.commit()

def all_recordings_in(dirpath: str) -> list[str]:
    all_files = []
    try:
        for f in os.listdir(dirpath):
            filepath = os.path.join(dirpath, f)

            if os.path.isdir(filepath):
                all_files += all_recordings_in(filepath)
                continue

            if not os.path.isfile(filepath):
                continue

            if f.endswith(E2_VIDEO_EXTENSION):
                all_files.append(filepath)
    except PermissionError:
        pass

    return all_files

def main(argc: int, argv: list[str]) -> None:
    if argc < 2:
        raise IndexError(f"Usage: {argv[0]} <dir path> [dir path ...]")

    db_init()

    print("Scanning directories... (This may take a while)", file=sys.stderr)

    filenames = []
    for i, d in enumerate(argv[1:]):
        print(f"Scanning directory: {i + 1} of {argc - 1}", end="\r", file=sys.stderr)
        filenames += all_recordings_in(d)

    print(f"Successfully scanned {argc - 1} directories.", file=sys.stderr)

    print("Processing recordings... (This may take a while)", file=sys.stderr)

    db_count = 0
    for i, f in enumerate(filenames):
        print(f"Processing recording {i + 1} of {len(filenames)}", end="\r", file=sys.stderr)
        basepath = re.sub(f"\{E2_VIDEO_EXTENSION}$", "", f)
        rec = RecordingFactory.from_database(basepath)
        if rec is not None:
            recordings.append(rec)
            db_count += 1
            continue
        try:
            with open(basepath + E2_META_EXTENSION, "r", encoding="utf-8") as m:
                rec = RecordingFactory.from_meta_file(basepath, m.readlines())
                db_save(rec)
                recordings.append(rec)
        except FileNotFoundError:
            print(f"{f}.meta not found! Skipping...", file=sys.stderr)

    print(f"Successfully processed {len(filenames)} recordings. ({db_count} in cache, {len(filenames) - db_count} new)", file=sys.stderr)

    radios_metadata = (("groupkey", QueryType.ATTRIBUTE), SortOrder.ASC)
    sort_recordings(radios_metadata[0][0], radios_metadata[0][1], radios_metadata[1])
    radios_metadata_previous = radios_metadata

    gui_init()

    while True:
        selected_recodings = [r for r in recordings if r.is_dropped]
        good_recodings = [r for r in recordings if r.is_good]
        mastered_recodings = [r for r in recordings if r.is_mastered]

        radios_metadata = tuple(r.metadata for r in window.element_list() if isinstance(r, sg.Radio) and r.get())
        if isinstance(radios_metadata[0], SortOrder):
            radios_metadata = radios_metadata[::-1]

        if radios_metadata != radios_metadata_previous:
            recordingBox_selected_rec = window["recordingBox"].get()
            sort_recordings(radios_metadata[0][0], radios_metadata[0][1], radios_metadata[1])
            window["recordingBox"].update(recordings)
            if len(recordingBox_selected_rec) > 0:
                gui_reselect(recordingBox_selected_rec)
            radios_metadata_previous = radios_metadata



        window["informationTxt"].update(f"{len(selected_recodings)} item(s) (approx. {to_GiB(sum([r.file_size for r in selected_recodings])):.1f} GiB) selected for drop | {len(good_recodings)} recordings good | {len(mastered_recodings)} mastered | {len(recordings)} total")

        gui_recolor(window)
        event, _ = window.read()

        if event == sg.WIN_CLOSED:
            quit()

        recordingBox_selected_rec = window["recordingBox"].get()

        if len(recordingBox_selected_rec) > 0:
            r = recordingBox_selected_rec[0]
            window["metaTxt"].update(f"{r.video_width:4d}x{r.video_height:4d} @ {r.video_fps} fps")
            window["selectionTxt"].update(f"{len(recordingBox_selected_rec)} recording(s) under cursor")
            window["commentMul"].update(recordingBox_selected_rec[0].comment)

        # [C]omment
        if ((event == "c:54" and len(recordingBox_selected_rec) == 1)
        or ( event == "C:54" and len(recordingBox_selected_rec) >  0)):
            window["recordingBox"].update(disabled=True)
            window["dropBtn"].update(disabled=True)
            window["metaTxt"].update("COMMENT Mode | Submit: [ESC]")
            window["commentMul"].update(disabled=False)
            window["commentMul"].set_focus()

            while True:
                event, _ = window.read()

                if event == sg.WIN_CLOSED:
                    quit()

                if event != "Escape:9":
                    continue

                comment = window["commentMul"].get()
                break

            window["commentMul"].update(disabled=True)
            window["dropBtn"].update(disabled=False)
            window["metaTxt"].update("SELECT Mode")
            window["recordingBox"].update(disabled=False)
            update_attribute(recordingBox_selected_rec,
                             lambda r: True,
                             lambda r: setattr(r, "comment", comment))
            window["recordingBox"].set_focus()
            continue

        # [O]pen recording using VLC
        if event == "o:32" and len(recordingBox_selected_rec) > 0:
            subprocess.Popen(["/usr/bin/env", "vlc", recordingBox_selected_rec[0].basepath + E2_VIDEO_EXTENSION])
            continue

        # Select for [D]rop
        if event == "d:40":
            update_attribute(recordingBox_selected_rec,
                             lambda r: not r.is_mastered,
                             lambda r: setattr(r, "is_dropped", True))
            continue

        if event == "D:40":
            update_attribute(recordingBox_selected_rec,
                             lambda r: r.is_dropped ,
                             lambda r: setattr(r, "is_dropped", False))
            continue

        # Mark recording as [G]ood
        if event == "g:42":
            update_attribute(recordingBox_selected_rec,
                             lambda r: not r.is_good,
                             lambda r: setattr(r, "is_good", True))
            continue

        if event == "G:42":
            update_attribute(recordingBox_selected_rec,
                             lambda r: r.is_good,
                             lambda r: setattr(r, "is_good", False))
            continue

        # Mark recording as [M]astered
        if event == "m:58":
            update_attribute(recordingBox_selected_rec,
                             lambda r: not r.is_dropped,
                             lambda r: setattr(r, "is_mastered", True))
            continue

        if event == "M:58":
            update_attribute(recordingBox_selected_rec,
                             lambda r: r.is_mastered,
                             lambda r: setattr(r, "is_mastered", False))
            continue

        # Drop button pressed
        if event == "dropBtn":
            for_deletion = set()
            for r in [x for x in recordings if x.is_dropped]:
                drop_recording(r)
                for_deletion.add(r)
            for r in for_deletion:
                recordings.remove(r)
            window["recordingBox"].update(recordings)

if __name__ == "__main__":
    main(len(sys.argv), sys.argv)
