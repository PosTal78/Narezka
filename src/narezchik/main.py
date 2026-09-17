"""Stage-one desktop application."""
from __future__ import annotations
import json, sys, threading
from pathlib import Path
import tempfile
from PySide6.QtCore import QObject, QThread, Signal, QUrl, Qt
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox, QPushButton, QPlainTextEdit, QProgressBar, QSlider, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget
from narezchik.core import ProjectWorkflow, WorkflowError
from narezchik.app_paths import app_paths
from narezchik.models import ProjectFormatError, SceneState, SegmentMatch, SegmentStatus, SubtitleState, TimelineState, TTSSettings, VideoFragment, VideoIndexState, VideoSource
from narezchik.services import (INDEX_MODEL_VERSION, PARTIAL_INDEX_FILENAME, SegmentationMode, build_index, confirm_all_matches,
                                discard_index_revision, ensure_current_sources,
                                ffprobe_is_available, inspect_video, local_captioner, local_embedder, read_index, read_scenes, read_subtitles,
                                read_text_file, select_matches, write_canonical, TimelineError, add_fragment, autofill, create_from_matches, export, inspect_videos, load_timeline, remove_fragment, replace_fragment, save_timeline, synchronize_timeline, validate_timeline)
from narezchik.services.analysis import detect_scenes, Scene, transcribe, write_scenes
from narezchik.services.subtitles import SubtitleCue
from narezchik.services.tts import EdgeTTSService, TTSQueue

STATUS_LABELS = {SegmentStatus.READY: 'Готово', SegmentStatus.NEEDS_TTS: 'Нужно озвучить', SegmentStatus.STALE: 'Нужно переозвучить', SegmentStatus.EXCLUDED: 'Исключено', SegmentStatus.FILE_MISSING: 'Аудиофайл не найден', SegmentStatus.ERROR: 'Ошибка'}

class Worker(QObject):
    progress=Signal(int,int,str); done=Signal(object)
    def __init__(self, q, flow, project, root, force, only_ids=None): super().__init__(); self.q,self.flow,self.project,self.root,self.force,self.only_ids=q,flow,project,root,force,only_ids; self.stop=threading.Event()
    def run(self):
        result=self.q.generate(self.project,self.root,force=self.force,only_ids=self.only_ids,cancelled=self.stop.is_set,progress=lambda a,b,c:self.progress.emit(a,b,c),saved=lambda:self.flow.save(self.project,self.root)); self.done.emit(result)

class VideoWorker(QObject):
    progress=Signal(object,object,str); done=Signal(object,object)
    def __init__(self, action): super().__init__(); self.action=action; self.stop=threading.Event()
    def run(self):
        try:self.done.emit(self.action(self.stop.is_set,lambda a,b,t:self.progress.emit(a,b,t)),None)
        except Exception as error:self.done.emit(None,error)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__(); self.paths=app_paths(); self.paths.ensure_data_directories(); self.flow=ProjectWorkflow(); self.project=None; self.root=None; self.tts=EdgeTTSService(); self.worker=None; self.thread=None; self.video_worker=None; self.video_thread=None; self.video_finished=None;self.video_error_label=None; self.applying_settings=False;self.preview_fragments=[];self.preview_fragment_index=0
        self.video_part_index=0;self.video_offset=0.0;self.setting_video_source=False
        self.player=QMediaPlayer(self); self.player.setAudioOutput(QAudioOutput(self)); self.video_player=QMediaPlayer(self); self.video_audio=QAudioOutput(self); self.video_player.setAudioOutput(self.video_audio); self.setWindowTitle('Narezchik'); self.resize(1050,720); self.build()
    def build(self):
        root=QWidget(); box=QVBoxLayout(root); top=QHBoxLayout(); self.title=QLabel('Проект не выбран — несохранённый черновик'); top.addWidget(self.title,1)
        for label,fn in [('Новый проект',self.new),('Выбрать проект',self.open),('Открыть папку',self.folder),('Диагностика',self.diagnostics)]: b=QPushButton(label);b.clicked.connect(fn);top.addWidget(b)
        box.addLayout(top); self.tabs=QTabWidget(); self.tabs.addTab(self.voice_tab(),'Озвучка'); self.tabs.addTab(self.video_tab(),'Видео'); self.tabs.addTab(self.match_tab(),'Подбор кадров')
        self.tabs.addTab(self.montage_tab(),'Монтаж'); self.tabs.addTab(self.export_tab(),'Экспорт')
        self.tabs.currentChanged.connect(self.tab_changed); box.addWidget(self.tabs); self.setCentralWidget(root)
    def voice_tab(self):
        w=QWidget(); b=QVBoxLayout(w); row=QHBoxLayout(); self.mode=QComboBox()
        for label,value in [('По строкам',SegmentationMode.LINES),('По предложениям',SegmentationMode.SENTENCES),('По абзацам',SegmentationMode.PARAGRAPHS)]: self.mode.addItem(label,value)
        for label,fn in [('Импорт TXT',self.import_txt),('Очистить',self.clear),('Разбить на сегменты',self.segment)]: x=QPushButton(label);x.clicked.connect(fn);row.addWidget(x)
        row.addWidget(self.mode);b.addLayout(row);self.text=QPlainTextEdit();self.text.setPlaceholderText('Вставьте сценарий или импортируйте TXT.');b.addWidget(self.text,2)
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(['ID','Текст','Статус','Длительность','Озвучивать']);b.addWidget(self.table,3);actions=QHBoxLayout()
        for label,fn in [('Правка',self.edit),('Разделить',self.split),('Склеить со следующим',self.merge),('Удалить',self.delete),('Вкл/искл',self.exclude),('Слушать',self.play),('Перегенерировать',self.regenerate),('Озвучить всё',lambda:self.generate(False)),('Полная перегенерация',lambda:self.generate(True))]: x=QPushButton(label);x.clicked.connect(fn);actions.addWidget(x)
        self.cancel=QPushButton('Остановить после текущей');self.cancel.setEnabled(False);self.cancel.clicked.connect(lambda:self.worker.stop.set() if self.worker else None);actions.addWidget(self.cancel);self.progress=QProgressBar();actions.addWidget(self.progress);b.addLayout(actions)
        form=QFormLayout();self.lang=QComboBox();self.lang.setEditable(True);self.lang.addItem('ru-RU');self.voice=QComboBox();self.voice.setEditable(True);self.voice.addItem('ru-RU-DmitryNeural')
        self.rate=QComboBox();self.rate.setEditable(True);self.rate.addItems(['-20%','-10%','+0%','+10%']);self.rate.setCurrentText('+0%');self.volume=QComboBox();self.volume.setEditable(True);self.volume.addItems(['-20%','+0%','+10%']);self.volume.setCurrentText('+0%');self.pitch=QComboBox();self.pitch.setEditable(True);self.pitch.addItems(['-5Hz','+0Hz','+5Hz']);self.pitch.setCurrentText('+0Hz');self.sample=QPlainTextEdit('Проверяю голос, скорость и произношение.');self.sample.setMaximumHeight(48)
        refresh=QPushButton('Обновить голоса');refresh.clicked.connect(self.voices);apply=QPushButton('Применить настройки');apply.clicked.connect(self.apply_tts_settings);preview=QPushButton('Проверить голос');preview.clicked.connect(self.preview)
        for label,x in [('Язык',self.lang),('Голос',self.voice),('',refresh),('Скорость',self.rate),('Громкость',self.volume),('Высота',self.pitch),('',apply),('Текст проверки',self.sample),('',preview)]:form.addRow(label,x)
        b.addLayout(form);return w
    def video_tab(self):
        w=QWidget(); box=QVBoxLayout(w); row=QHBoxLayout(); self.import_video_button=QPushButton('Выбрать части фильма');self.import_video_button.clicked.connect(self.choose_video);row.addWidget(self.import_video_button)
        self.open_external_button=QPushButton('Открыть внешним проигрывателем');self.open_external_button.clicked.connect(self.open_external_video);row.addWidget(self.open_external_button);self.video_status=QLabel('Исходный фильм не выбран.');row.addWidget(self.video_status,1);box.addLayout(row)
        part_controls=QHBoxLayout();self.video_parts=QListWidget();self.video_parts.setMaximumHeight(82);part_controls.addWidget(self.video_parts,1)
        for label,delta in [('Выше',-1),('Ниже',1)]:button=QPushButton(label);button.clicked.connect(lambda _checked=False,d=delta:self.move_video_part(d));part_controls.addWidget(button)
        self.remove_video_part_button=QPushButton('Удалить часть');self.remove_video_part_button.clicked.connect(self.remove_video_part);part_controls.addWidget(self.remove_video_part_button);box.addLayout(part_controls)
        self.video_workflow_hint=QLabel('Порядок работы: выберите фильм → импортируйте SRT/VTT или нажмите «Расшифровать локально» → найдите сцены → перейдите на «Подбор кадров».');self.video_workflow_hint.setWordWrap(True);box.addWidget(self.video_workflow_hint)
        self.video_metadata=QLabel('');box.addWidget(self.video_metadata);self.video_widget=QVideoWidget();self.video_widget.setMinimumHeight(280);self.video_player.setVideoOutput(self.video_widget);box.addWidget(self.video_widget,1)
        controls=QHBoxLayout();self.video_play=QPushButton('Воспроизвести');self.video_play.clicked.connect(self.toggle_video);controls.addWidget(self.video_play);self.video_seek=QSlider(Qt.Horizontal);self.video_seek.sliderMoved.connect(self.seek_video);controls.addWidget(self.video_seek,1);self.video_mute=QPushButton('Без звука');self.video_mute.clicked.connect(lambda:self.video_audio.setMuted(not self.video_audio.isMuted()));controls.addWidget(self.video_mute);box.addLayout(controls)
        self.video_player.positionChanged.connect(self.video_position_changed);self.video_player.mediaStatusChanged.connect(self.video_media_status_changed);self.video_player.errorOccurred.connect(lambda *_:self.video_status.setText('Встроенный просмотр недоступен для этого кодека. Можно открыть фильм внешним проигрывателем.'))
        sub=QHBoxLayout();self.import_subtitles_button=QPushButton('Импорт SRT/VTT');self.import_subtitles_button.clicked.connect(self.import_subtitles);sub.addWidget(self.import_subtitles_button);self.transcribe_button=QPushButton('Расшифровать локально');self.transcribe_button.clicked.connect(self.start_transcription);sub.addWidget(self.transcribe_button);self.whisper_model=QComboBox();self.whisper_model.addItems(['сбалансированно','быстрее','точнее']);self.whisper_model.currentTextChanged.connect(self.update_whisper_hint);sub.addWidget(self.whisper_model);self.whisper_language=QComboBox();self.whisper_language.setEditable(True);self.whisper_language.addItems(['Авто','ru','en']);sub.addWidget(self.whisper_language);box.addLayout(sub);self.whisper_hint=QLabel();self.update_whisper_hint(self.whisper_model.currentText());box.addWidget(self.whisper_hint)
        scenes=QHBoxLayout();self.scenes_button=QPushButton('Найти сцены');self.scenes_button.clicked.connect(self.start_scene_detection);scenes.addWidget(self.scenes_button);self.video_cancel=QPushButton('Отменить');self.video_cancel.clicked.connect(lambda:self.video_worker.stop.set() if self.video_worker else None);self.video_cancel.setEnabled(False);scenes.addWidget(self.video_cancel);self.video_progress=QProgressBar();scenes.addWidget(self.video_progress,1);box.addLayout(scenes);return w
    def match_tab(self):
        w=QWidget(); box=QVBoxLayout(w); row=QHBoxLayout()
        self.match_start=QPushButton('Подготовить и подобрать кадры');self.match_start.clicked.connect(lambda:self.start_matching(False));row.addWidget(self.match_start)
        self.match_refresh_button=QPushButton('Обновить анализ');self.match_refresh_button.clicked.connect(lambda:self.start_matching(True));row.addWidget(self.match_refresh_button)
        self.match_confirm_all=QPushButton('Подтвердить всё');self.match_confirm_all.clicked.connect(self.confirm_all_matches);row.addWidget(self.match_confirm_all)
        self.match_status=QLabel('Выберите фильм, подготовьте субтитры и сцены.');row.addWidget(self.match_status,1);box.addLayout(row)
        self.match_table=QTableWidget(0,5);self.match_table.setHorizontalHeaderLabels(['ID','Реплика','Статус','Уверенность','Фрагменты']);self.match_table.itemSelectionChanged.connect(self.refresh_match_preview);box.addWidget(self.match_table,3)
        lower=QHBoxLayout(); self.match_thumbnail=QLabel('Миниатюра появится после анализа.');self.match_thumbnail.setMinimumWidth(300);self.match_thumbnail.setMinimumHeight(170);lower.addWidget(self.match_thumbnail)
        actions=QVBoxLayout();self.match_preview=QPushButton('Просмотреть выбранный вариант');self.match_preview.clicked.connect(self.preview_match);actions.addWidget(self.match_preview);self.match_next=QPushButton('Следующий вариант');self.match_next.clicked.connect(self.next_match_candidate);actions.addWidget(self.match_next);self.match_choose=QPushButton('Закрепить выбранный вариант');self.match_choose.clicked.connect(self.choose_match);actions.addWidget(self.match_choose);self.match_details=QLabel('');self.match_details.setWordWrap(True);actions.addWidget(self.match_details);actions.addStretch();lower.addLayout(actions,1);box.addLayout(lower);self.match_candidate_index=0;self.match_candidate_segment=None;self.match_showing_candidate=False;return w
    def montage_tab(self):
        w=QWidget(); box=QVBoxLayout(w); row=QHBoxLayout(); self.timeline_status=QLabel('Создайте монтаж из результатов подбора.');row.addWidget(self.timeline_status,1)
        for label,fn in [('Обновить из проекта',self.timeline_synchronize),('Слушать озвучку',self.timeline_play_audio),('Просмотреть строку',self.timeline_preview),('Автодобрать',self.timeline_autofill),('Подтвердить строку',self.timeline_confirm)]: b=QPushButton(label);b.clicked.connect(fn);row.addWidget(b)
        box.addLayout(row);self.timeline_table=QTableWidget(0,5);self.timeline_table.setHorizontalHeaderLabels(['ID','Реплика','Аудио','Видео','Статус']);self.timeline_table.itemSelectionChanged.connect(self.refresh_timeline_selection);box.addWidget(self.timeline_table,2)
        self.timeline_fragments=QTableWidget(0,3);self.timeline_fragments.setHorizontalHeaderLabels(['Фрагмент','Начало','Конец']);self.timeline_fragments.itemSelectionChanged.connect(self.refresh_fragment_selection);box.addWidget(self.timeline_fragments,1)
        edit=QHBoxLayout();self.cut_start=QLineEdit();self.cut_start.setPlaceholderText('Начало, с');self.cut_end=QLineEdit();self.cut_end.setPlaceholderText('Конец, с')
        for label,fn in [('Взять начало из проигрывателя',self.mark_cut_start),('Взять конец из проигрывателя',self.mark_cut_end),('Добавить кусок',self.timeline_add),('Заменить выбранный',self.timeline_replace),('Удалить выбранный',self.timeline_remove)]:b=QPushButton(label);b.clicked.connect(fn);edit.addWidget(b)
        edit.addWidget(self.cut_start);edit.addWidget(self.cut_end);box.addLayout(edit);hint=QLabel('Перемотайте фильм на вкладке «Видео» и возьмите метки либо введите секунды вручную. Выбранный кусок можно заменить или удалить; последний кусок подрезается до длительности озвучки.');hint.setWordWrap(True);box.addWidget(hint);self.timeline=None;return w
    def export_tab(self):
        w=QWidget(); box=QVBoxLayout(w); form=QFormLayout();self.export_resolution=QComboBox();self.export_resolution.addItems(['Original','1080p','720p']);self.export_fps=QComboBox();self.export_fps.addItems(['Original','30','60']);self.export_audio=QComboBox();self.export_audio.addItems(['Оригинальный звук выключен','Тихий фон фильма']);form.addRow('Разрешение',self.export_resolution);form.addRow('FPS',self.export_fps);form.addRow('Звук фильма',self.export_audio);box.addLayout(form);self.export_status=QLabel('Экспорт доступен после проверки монтажа.');self.export_status.setWordWrap(True);box.addWidget(self.export_status);self.export_button=QPushButton('Экспортировать final.mp4');self.export_button.clicked.connect(self.start_export);box.addWidget(self.export_button);box.addStretch();return w
    def tab_changed(self,index):
        if self.tabs.tabText(index)=='Видео':self.refresh_video(check_availability=True)
        if self.tabs.tabText(index)=='Подбор кадров':self.refresh_matching()
        if self.tabs.tabText(index)=='Монтаж':self.refresh_timeline()
        if self.tabs.tabText(index)=='Экспорт':self.refresh_export()
    def update_whisper_hint(self,choice):
        hints={'быстрее':'Быстрее: небольшая модель, меньше места, ниже точность.', 'сбалансированно':'Сбалансированно: рекомендуемая модель; первая загрузка займёт место на диске и время.', 'точнее':'Точнее: более крупная модель, дольше обработка и больше места.'}
        self.whisper_hint.setText(hints[choice]+' Модель сохраняется в data/models/whisper; при неполной настройке NVIDIA расшифровка автоматически продолжится на CPU.')
    def refresh_video(self,check_availability=False):
        source=self.project.video_source if self.project else None
        if not source:
            self.video_player.stop();self.video_player.setSource(QUrl());self.video_seek.setValue(0);self.video_play.setText('Воспроизвести')
            self.video_parts.clear();self.video_status.setText('Исходный фильм не выбран.');self.video_metadata.setText('');return
        parts=source.source_parts;available=all(Path(part.path).is_file() for part in parts)
        self.video_status.setText('Все части фильма найдены.' if available else 'Одна из частей фильма не найдена. Выберите состав фильма заново.')
        self.video_metadata.setText(f'{len(parts)} част. — всего {source.duration:.2f} с, {source.width}×{source.height}, {source.fps:.3g} FPS')
        self.video_parts.clear()
        for number,part in enumerate(parts,1):self.video_parts.addItem(f'{number}. {part.file_name} — {part.duration:.1f} с')
        self.video_seek.setMaximum(round(source.duration*1000))
        wanted=QUrl.fromLocalFile(parts[self.video_part_index if self.video_part_index<len(parts) else 0].path)
        if available and self.video_player.source()!=wanted:self.set_video_part(0,0)
        if not available:self.video_player.stop();self.video_player.setSource(QUrl());self.video_seek.setValue(0);self.video_play.setText('Воспроизвести')
    def choose_video(self):
        if not self.need() or self.video_worker or self.worker:return
        paths,_=QFileDialog.getOpenFileNames(self,'Части фильма в порядке воспроизведения',filter='Видео (*.mp4 *.mkv)')
        if paths:self.start_video_job('Проверяю части фильма…',lambda stopped,report:inspect_videos([Path(path) for path in paths],cancelled=stopped,progress=lambda a,b:report(a,b,'Проверяю содержимое файлов')),self.finish_video_import)
    def start_video_job(self,label,action,finished,error_label=None):
        if self.worker or self.video_worker:return
        self.video_status.setText(label);self.video_cancel.setEnabled(True);self.tabs.widget(0).setEnabled(False);self.tabs.widget(3).setEnabled(False);self.export_button.setEnabled(False);self.import_video_button.setEnabled(False);self.import_subtitles_button.setEnabled(False);self.transcribe_button.setEnabled(False);self.scenes_button.setEnabled(False);self.video_thread=QThread(self);self.video_worker=VideoWorker(action);self.video_finished=finished;self.video_error_label=error_label;self.video_worker.moveToThread(self.video_thread);self.video_thread.started.connect(self.video_worker.run);self.video_worker.progress.connect(self.update_video_progress);self.video_worker.done.connect(self.finish_video_job);self.video_thread.start()
    def update_video_progress(self,current,total,text):
        if total and total > 2_000_000_000:
            self.video_progress.setMaximum(1000);self.video_progress.setValue(min(1000,int(current*1000/total)));self.video_progress.setFormat(f'{text} ({current/total:.0%})')
        else:self.video_progress.setMaximum(max(1,total));self.video_progress.setValue(current);self.video_progress.setFormat(text)
    def finish_video_job(self,result,error):
        finished=self.video_finished;error_label=self.video_error_label;self.video_finished=None;self.video_error_label=None
        self.video_thread.quit();self.video_thread.wait();self.video_worker=None;self.video_thread=None;self.video_cancel.setEnabled(False);self.tabs.widget(0).setEnabled(True);self.tabs.widget(3).setEnabled(True);self.import_video_button.setEnabled(True);self.import_subtitles_button.setEnabled(True);self.transcribe_button.setEnabled(True);self.scenes_button.setEnabled(True)
        self.export_button.setEnabled(bool(self.project and self.timeline and self.root and not validate_timeline(self.project,self.timeline,self.root)))
        if error:self.video_status.setText(str(error));(error_label or self.video_status).setText(str(error));return
        try:finished(result)
        except Exception as error:
            self.video_status.setText(f'Операция не завершена: {error}')
    def finish_video_import(self,source):
        different=self.project.video_source and self.project.video_source.fingerprint!=source.fingerprint
        if different and QMessageBox.question(self,'Другой фильм','Этот файл отличается от прежнего. Активные субтитры и сцены будут архивированы. Продолжить?',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:self.refresh_video();return
        self.flow.replace_video_source(self.project,self.root,source,archive_previous=bool(different));self.video_part_index=0;self.video_offset=0;self.refresh_video();self.statusBar().showMessage('Состав исходного фильма сохранён.')
        if different:
            self.timeline=None
    def open_external_video(self):
        if self.project and self.project.video_source:
            index=self.video_parts.currentRow();index=index if index>=0 else self.video_part_index
            part=self.project.video_source.source_parts[min(index,len(self.project.video_source.source_parts)-1)]
            if Path(part.path).is_file():__import__('os').startfile(part.path)
    def toggle_video(self):
        if self.video_player.playbackState()==QMediaPlayer.PlayingState:self.video_player.pause();self.video_play.setText('Воспроизвести')
        else:self.video_player.play();self.video_play.setText('Пауза')
    def set_video_part(self,index,local_position=0,play=False):
        if not self.project or not self.project.video_source:return
        parts=self.project.video_source.source_parts
        if not 0<=index<len(parts):return
        self.setting_video_source=True;self.video_part_index=index;self.video_offset=self.project.video_source.offsets[index]
        self.video_player.setSource(QUrl.fromLocalFile(parts[index].path));self.video_player.setPosition(round(local_position*1000));self.setting_video_source=False
        if play:self.video_player.play()
    def seek_video(self,global_position):
        if not self.project or not self.project.video_source:return
        index,_part,local=self.project.video_source.locate(global_position/1000)
        if index!=self.video_part_index:self.set_video_part(index,local,self.video_player.playbackState()==QMediaPlayer.PlayingState)
        else:self.video_player.setPosition(round(local*1000))
    def video_position_changed(self,position):
        global_position=round(self.video_offset*1000)+position;self.video_seek.setValue(global_position);self.advance_match_preview(global_position)
    def video_media_status_changed(self,status):
        if status==QMediaPlayer.EndOfMedia and not self.preview_fragments and self.project and self.project.video_source:
            if self.video_part_index+1<len(self.project.video_source.source_parts):self.set_video_part(self.video_part_index+1,0,True)
            else:self.video_play.setText('Воспроизвести')
    def move_video_part(self,delta):
        if not self.project or not self.project.video_source:return
        parts=list(self.project.video_source.source_parts);row=self.video_parts.currentRow()
        target=row+delta
        if row<0 or not 0<=target<len(parts):return
        parts[row],parts[target]=parts[target],parts[row];self.apply_video_parts(parts);self.video_parts.setCurrentRow(target)
    def remove_video_part(self):
        if not self.project or not self.project.video_source:return
        parts=list(self.project.video_source.source_parts);row=self.video_parts.currentRow()
        if row<0 or len(parts)==1:return
        parts.pop(row);self.apply_video_parts(parts)
    def apply_video_parts(self,parts):
        source=VideoSource.combine(parts)
        if source.fingerprint==self.project.video_source.fingerprint:return
        if QMessageBox.question(self,'Изменить состав фильма','Порядок или состав частей изменится. Активный анализ будет архивирован. Продолжить?',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:self.refresh_video();return
        self.flow.replace_video_source(self.project,self.root,source,archive_previous=True);self.timeline=None;self.video_part_index=0;self.refresh_video()
    def transcribe_source(self,source,model,language,stopped,report):
        cues=[];detected=[]
        for index,(part,offset) in enumerate(zip(source.source_parts,source.offsets),1):
            report(index-1,len(source.source_parts),f'Расшифровываю часть {index}/{len(source.source_parts)}')
            part_cues,part_language=transcribe(Path(part.path),model_choice=model,language=language,cancelled=stopped,progress=lambda a,b,t:report(a,b,f'Часть {index}: {t}'))
            cues.extend(SubtitleCue(cue.start+offset,cue.end+offset,cue.text) for cue in part_cues);detected.append(part_language)
        return cues,(detected[0] if detected and all(item==detected[0] for item in detected) else (language or 'mixed'))
    def detect_source_scenes(self,source,stopped,report=None):
        result=[];identifier=1
        for index,(part,offset) in enumerate(zip(source.source_parts,source.offsets)):
            if report:report(index,len(source.source_parts),f'Ищу сцены в части {index+1}/{len(source.source_parts)}')
            for scene in detect_scenes(Path(part.path),part.duration,cancelled=stopped):
                result.append(Scene(identifier,scene.start+offset,scene.end+offset,index));identifier+=1
        return result
    def index_inputs(self,source):
        return [(Path(part.path),offset,part.fingerprint) for part,offset in zip(source.source_parts,source.offsets)]
    def import_subtitles(self):
        if not self.project or not self.project.video_source or self.video_worker or self.worker:return
        path,_=QFileDialog.getOpenFileName(self,'Субтитры',filter='Субтитры (*.srt *.vtt)')
        if not path:return
        source=self.project.video_source
        self.start_video_job('Проверяю фильм и субтитры…',lambda stopped,report:(ensure_current_sources(source,cancelled=stopped,progress=lambda a,b:report(a,b,'Проверяю содержимое файлов')),read_subtitles(Path(path)))[1],lambda cues:self.finish_subtitle_import(cues,source))
    def finish_subtitle_import(self,cues,source):
        sample=' / '.join(cue.text.replace('\n',' ')[:45] for cue in cues[:2]);last=cues[-1].end;message=f'Реплик: {len(cues)}\nПоследний таймкод: {last:.1f} с\nФильм: {source.duration:.1f} с\nПример: {sample}'
        if abs(last-source.duration)>30:message+='\n\nВозможное расхождение таймингов. Импортировать всё равно?'
        if QMessageBox.question(self,'Проверка субтитров',message,QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
        self.replace_subtitles(cues, source.fingerprint, 'import')
    def start_transcription(self):
        if not self.project or not self.project.video_is_available or self.video_worker or self.worker:return
        source=self.project.video_source;language=self.whisper_language.currentText().strip();language=None if language=='Авто' else language;model=self.whisper_model.currentText()
        if self.project.active_subtitles and self.project.active_subtitles.kind=='whisper' and self.project.active_subtitles.source_fingerprint==source.fingerprint and self.project.active_subtitles.model==model and (language is None or self.project.active_subtitles.language==language) and (self.root/self.project.active_subtitles.subtitles_path).is_file() and (self.root/self.project.active_subtitles.transcript_path).is_file():self.video_status.setText('Используется сохранённая расшифровка.');return
        self.start_video_job('Проверяю исходный фильм…',lambda stopped,report:(ensure_current_sources(source,cancelled=stopped,progress=lambda a,b:report(a,b,'Проверяю содержимое файлов')),self.transcribe_source(source,model,language,stopped,report))[1],lambda result:self.finish_transcription(result,source,model))
    def finish_transcription(self,result,source,model):
        cues,language=result;self.replace_subtitles(cues, source.fingerprint, 'whisper', language, model)
    def replace_subtitles(self,cues,fingerprint,kind,language=None,model=None):
        staging=Path(tempfile.mkdtemp(prefix='.subtitles-',dir=self.root/'analysis'))
        try:
            srt,transcript=write_canonical(cues,staging,language=language,kind=kind,model=model)
            state=SubtitleState('analysis/subtitles.srt','analysis/transcript.json',fingerprint,kind,language,model)
            self.flow.replace_subtitles(self.project,self.root,state,srt,transcript)
        except Exception as error:
            QMessageBox.warning(self,'Субтитры',f'Не удалось заменить субтитры. Прежний вариант сохранён.\n{error}')
            return False
        finally:
            if staging.exists():
                for item in staging.iterdir(): item.unlink(missing_ok=True)
                staging.rmdir()
        self.video_status.setText('Активные субтитры обновлены.' if kind=='import' else 'Локальная расшифровка завершена.')
        return True
    def start_scene_detection(self):
        if not self.project or not self.project.video_is_available or self.video_worker or self.worker:return
        source=self.project.video_source
        if self.project.scene_analysis and self.project.scene_analysis.source_fingerprint==source.fingerprint and (self.root/self.project.scene_analysis.scenes_path).is_file():self.video_status.setText('Используются сохранённые границы сцен.');return
        self.start_video_job('Проверяю исходный фильм…',lambda stopped,report:(ensure_current_sources(source,cancelled=stopped,progress=lambda a,b:report(a,b,'Проверяю содержимое файлов')),self.detect_source_scenes(source,stopped,report))[1],lambda scenes:self.finish_scenes(scenes,source))
    def finish_scenes(self,scenes,source):
        destination=write_scenes(scenes,self.root/'analysis',source.fingerprint);self.flow.activate_scenes(self.project,self.root,SceneState(str(destination.relative_to(self.root)).replace('\\','/'),source.fingerprint));self.video_status.setText('Границы сцен сохранены.' if len(scenes)>1 else 'Границы сцен не найдены: фильм сохранён одной сценой.')
    def start_matching(self,force):
        if not self.project or not self.project.video_is_available or self.video_worker or self.worker:return
        source=self.project.video_source
        need_subtitles=not self.project.active_subtitles
        need_scenes=not self.project.scene_analysis
        if (need_subtitles or need_scenes) and QMessageBox.question(self,'Подготовка фильма','Не хватает: '+(', '.join(name for name,missing in [('субтитры',need_subtitles),('границы сцен',need_scenes)] if missing))+'.\n\nПодготовить автоматически и начать подбор?',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
        if force and QMessageBox.question(self,'Обновить анализ','Прежние автоматические предложения будут обновлены. Закреплённые вручную варианты сохранятся.',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
        if not force and self.project.video_index and self.project.video_index.model_version==INDEX_MODEL_VERSION and (self.root/self.project.video_index.index_path).is_file():
            def reuse_index(stopped,report):
                ensure_current_sources(source,cancelled=stopped,progress=lambda a,b:report(a,b,'Проверяю содержимое фильма'))
                return select_matches(self.project.segments,read_index(self.root/self.project.video_index.index_path,source.fingerprint,source_duration=source.duration),embedder=local_embedder(),manual_matches=self.project.segment_matches)
            self.start_video_job('Обновляю подбор по сохранённому анализу…',reuse_index,self.finish_cached_matching,self.match_status);return
        partial_index=self.root/'analysis'/PARTIAL_INDEX_FILENAME
        resuming=partial_index.is_file()
        self.match_status.setText('Продолжаю незавершённый анализ фильма.' if resuming else 'Готовлю локальный анализ. Первая загрузка модели может занять время и место на диске.')
        whisper_model=self.whisper_model.currentText();whisper_language=self.whisper_language.currentText().strip();whisper_language=None if whisper_language=='Авто' else whisper_language
        def action(stopped,report):
            ensure_current_sources(source,cancelled=stopped,progress=lambda a,b:report(a,b,'Проверяю содержимое фильма'))
            embed=local_embedder();caption=local_captioner();report(0,0,f'Визуальный анализ: {getattr(caption,"device_label","CPU")}')
            transcript=read_subtitles(self.root/self.project.active_subtitles.subtitles_path) if self.project.active_subtitles else self.transcribe_source(source,whisper_model,whisper_language,stopped,report)
            cues=transcript if self.project.active_subtitles else transcript[0]
            detected_language=None if self.project.active_subtitles else transcript[1]
            scenes=read_scenes(self.root/self.project.scene_analysis.scenes_path,source.fingerprint) if self.project.scene_analysis else self.detect_source_scenes(source,stopped,report)
            index_path=build_index(self.index_inputs(source),scenes,cues,self.root/'analysis',source.fingerprint,captioner=caption,embedder=embed,cancelled=stopped,progress=report)
            try:
                matches=select_matches(self.project.segments,read_index(index_path,source.fingerprint,source_duration=source.duration),embedder=embed,manual_matches=self.project.segment_matches)
            except Exception:
                discard_index_revision(index_path)
                raise
            return cues,scenes,index_path,need_subtitles,need_scenes,matches,detected_language,whisper_model
        self.start_video_job('Подбираю кадры…',action,lambda result:self.finish_matching(result,source),self.match_status)
    def finish_matching(self,result,source):
        cues,scenes,index_path,created_subtitles,created_scenes,matches,detected_language,whisper_model=result
        previous_index=self.project.video_index;previous_matches=self.project.segment_matches
        if created_subtitles:
            staging=Path(tempfile.mkdtemp(prefix='.subtitles-',dir=self.root/'analysis'))
            try:
                srt,transcript=write_canonical(cues,staging,kind='whisper',language=detected_language,model=whisper_model)
                self.flow.replace_subtitles(self.project,self.root,SubtitleState('analysis/subtitles.srt','analysis/transcript.json',source.fingerprint,'whisper',detected_language,whisper_model),srt,transcript)
            finally:
                if staging.exists():
                    for item in staging.iterdir():item.unlink(missing_ok=True)
                    staging.rmdir()
        if created_scenes:
            destination=write_scenes(scenes,self.root/'analysis',source.fingerprint);self.flow.activate_scenes(self.project,self.root,SceneState(str(destination.relative_to(self.root)).replace('\\','/'),source.fingerprint))
        try:
            self.project.set_video_index(VideoIndexState(str(index_path.relative_to(self.root)).replace('\\','/'),source.fingerprint,INDEX_MODEL_VERSION))
            self.project.replace_segment_matches(matches)
            self.flow.save(self.project,self.root)
        except Exception:
            if created_subtitles or created_scenes:self.project.video_index=None;self.project.segment_matches=[]
            else:self.project.video_index=previous_index;self.project.segment_matches=previous_matches
            discard_index_revision(index_path)
            raise
        self.match_status.setText('Подбор завершён. Проверьте строки со статусом «Требует проверки».');self.refresh_matching()
    def finish_cached_matching(self,matches):
        previous=self.project.segment_matches
        try:self.project.replace_segment_matches(matches);self.flow.save(self.project,self.root)
        except Exception:self.project.segment_matches=previous;raise
        self.match_status.setText('Подбор обновлён по сохранённому анализу.');self.refresh_matching()
    def refresh_matching(self):
        if not hasattr(self,'match_table'):return
        matches={item.segment_id:item for item in self.project.segment_matches} if self.project else {}
        segments=self.project.segments if self.project else [];self.match_table.setRowCount(len(segments))
        for row,segment in enumerate(segments):
            item=matches.get(segment.segment_id);status='Ожидает подбора'
            if item:
                status=('Подтверждено массово' if item.confirmation=='bulk' else
                        ('Закреплено вручную' if item.confirmation=='manual' else ('Требует проверки' if item.needs_review else 'Предложено')))
                if item.reason:status+=': '+item.reason
                if item.context_used and not item.manual:status+='; учтён локальный контекст'
            confidence='' if not item or item.confidence is None else f'{item.confidence:.0%}'
            fragments='' if not item or not item.fragments else ', '.join(f'{part.start:.1f}–{part.end:.1f} с' for part in item.fragments)
            for column,value in enumerate([segment.segment_id,segment.text,status,confidence,fragments]):self.match_table.setItem(row,column,QTableWidgetItem(str(value)))
        self.match_start.setEnabled(bool(self.project and self.project.video_is_available and not self.video_worker));self.match_refresh_button.setEnabled(bool(self.project and self.project.video_index and not self.video_worker))
        self.match_confirm_all.setEnabled(bool(matches and not self.video_worker))
    def selected_match(self):
        if not self.project or self.match_table.currentRow()<0:return None
        segment=self.project.segments[self.match_table.currentRow()]
        return next((item for item in self.project.segment_matches if item.segment_id==segment.segment_id),None)
    def refresh_match_preview(self):
        match=self.selected_match()
        if not match or match.segment_id!=self.match_candidate_segment:
            self.match_candidate_segment=match.segment_id if match else None;self.match_candidate_index=0;self.match_showing_candidate=False
        elif match.candidates:self.match_candidate_index=min(self.match_candidate_index,len(match.candidates)-1)
        fragments=self.match_fragments(match)
        if not fragments:
            self.match_thumbnail.setText('Для этой реплики нет готового фрагмента.');self.match_thumbnail.setPixmap(QPixmap());self.match_details.setText(match.reason if match and match.reason else '');return
        first=fragments[0];details='Кандидат: '+', '.join(f'{item.start:.1f}–{item.end:.1f} с' for item in fragments)
        if match and match.reason:details+='\nПричина проверки: '+match.reason
        if match and match.context_used:details+='\nУчтён локальный контекст из вручную закреплённых кадров.'
        self.match_details.setText(details)
        path=self.root/first.thumbnail_path if self.root and first.thumbnail_path else None
        pixmap=QPixmap(str(path)) if path and path.is_file() else QPixmap()
        if pixmap.isNull():self.match_thumbnail.setText('Миниатюра недоступна.')
        else:self.match_thumbnail.setPixmap(pixmap.scaled(300,170,Qt.KeepAspectRatio,Qt.SmoothTransformation))
    def preview_match(self):
        match=self.selected_match()
        fragments=self.match_fragments(match)
        if fragments and self.project and self.project.video_source:
            self.preview_fragments=fragments;self.preview_fragment_index=0;self.play_preview_fragment();self.tabs.setCurrentIndex(1)
    def play_preview_fragment(self):
        fragment=self.preview_fragments[self.preview_fragment_index]
        index,_part,local=self.project.video_source.locate(fragment.start);self.set_video_part(index,local,True)
    def advance_match_preview(self,position):
        if self.preview_fragments and position>=round(self.preview_fragments[self.preview_fragment_index].end*1000):
            self.preview_fragment_index+=1
            if self.preview_fragment_index<len(self.preview_fragments):self.play_preview_fragment()
            else:self.video_player.pause();self.preview_fragments=[]
    def choose_match(self):
        match=self.selected_match()
        if not match or not match.candidates:return
        previous=(match.fragments,match.manual,match.confirmation,match.needs_review,match.reason)
        try:
            match.fragments=list(self.match_fragments(match));match.manual=True;match.confirmation='manual';match.needs_review=False;match.reason=None;self.flow.save(self.project,self.root)
        except Exception as error:
            match.fragments,match.manual,match.confirmation,match.needs_review,match.reason=previous;QMessageBox.warning(self,'Подбор кадров',f'Не удалось сохранить ручной выбор.\n{error}');return
        self.refresh_matching()
    def confirm_all_matches(self):
        if not self.project:return
        confirmable=[item for item in self.project.segment_matches if item.fragments or item.candidates]
        unresolved=[item for item in self.project.segment_matches if not item.fragments and not item.candidates]
        doubtful=[item for item in confirmable if item.needs_review]
        message=(f'Будут подтверждены первые доступные варианты: {len(confirmable)}.\n'
                 f'Из них требуют проверки: {len(doubtful)}.\nБез кандидатов: {len(unresolved)}.\n\nПродолжить?')
        if QMessageBox.question(self,'Подтвердить всё',message,QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
        previous=[(item,list(item.fragments),item.manual,item.confirmation,item.needs_review,item.reason) for item in confirmable]
        try:
            confirmed,unresolved_count=confirm_all_matches(self.project.segment_matches)
            self.flow.save(self.project,self.root)
        except Exception as error:
            for item,fragments,manual,confirmation,needs_review,reason in previous:
                item.fragments=fragments;item.manual=manual;item.confirmation=confirmation;item.needs_review=needs_review;item.reason=reason
            QMessageBox.warning(self,'Подтвердить всё',f'Не удалось сохранить изменения.\n{error}');return
        self.refresh_matching();self.statusBar().showMessage(f'Массово подтверждено: {confirmed}; без кандидатов: {unresolved_count}.')
    def match_fragments(self,match):
        if match and match.candidates and self.match_showing_candidate:return match.candidates[self.match_candidate_index]
        return match.fragments if match and match.fragments else (match.candidates[0] if match and match.candidates else [])
    def next_match_candidate(self):
        match=self.selected_match()
        if match and len(match.candidates)>1:
            self.match_candidate_segment=match.segment_id;self.match_showing_candidate=True;self.match_candidate_index=(self.match_candidate_index+1)%len(match.candidates);self.refresh_match_preview()
    def ensure_timeline(self):
        if not self.project or not self.root or not self.project.video_source:return False
        if self.timeline is None:
            try:self.timeline=load_timeline(self.root,self.project.video_source.fingerprint)
            except TimelineError as error:QMessageBox.warning(self,'Монтаж',str(error));return False
        if self.timeline is None:
            self.timeline=create_from_matches(self.project);self.save_timeline()
        self.refresh_timeline();return True
    def timeline_synchronize(self):
        if not self.project or not self.root or not self.project.video_source:return
        if self.timeline is None:
            try:self.timeline=load_timeline(self.root,self.project.video_source.fingerprint)
            except TimelineError as error:
                if QMessageBox.question(self,'Восстановить монтаж',f'{error}\n\nСоздать новый монтаж из текущего проекта?',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
                self.timeline=None
        before_filled=sum(bool(entry.fragments) for entry in self.timeline.entries) if self.timeline else 0
        if self.timeline is None:self.timeline=create_from_matches(self.project)
        else:
            try:
                scenes=(read_scenes(self.root/self.project.scene_analysis.scenes_path,self.project.video_source.fingerprint)
                        if self.project.scene_analysis else [])
                synchronize_timeline(self.project,self.timeline,scenes=scenes)
            except TimelineError as error:
                if QMessageBox.question(self,'Восстановить монтаж',f'{error}\n\nСоздать новый монтаж из текущего проекта?',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
                self.timeline=create_from_matches(self.project)
        self.save_timeline();self.refresh_timeline();after_filled=sum(bool(entry.fragments) for entry in self.timeline.entries)
        unresolved=sum(entry.needs_review for entry in self.timeline.entries)
        self.statusBar().showMessage(f'Монтаж обновлён: заполнено новых строк {max(0,after_filled-before_filled)}, осталось проверить {unresolved}.')
    def save_timeline(self):
        if not self.timeline or not self.project or not self.root:return
        path=save_timeline(self.root,self.timeline);self.project.timeline_state=TimelineState(str(path.relative_to(self.root)).replace('\\','/'),self.timeline.source_fingerprint);self.flow.save(self.project,self.root)
    def refresh_timeline(self):
        if not hasattr(self,'timeline_table'):return
        if self.project and self.project.video_source and self.timeline is None:
            try:self.timeline=load_timeline(self.root,self.project.video_source.fingerprint)
            except TimelineError as error:self.timeline_status.setText(str(error));return
        timeline=self.timeline
        self.timeline_table.setRowCount(len(timeline.entries) if timeline else 0)
        if not timeline:
            self.timeline_fragments.setRowCount(0);self.timeline_status.setText('Создайте монтаж из результатов подбора.');return
        segments={item.segment_id:item for item in self.project.segments};issues=validate_timeline(self.project,timeline,self.root)
        for row,entry in enumerate(timeline.entries):
            segment=segments.get(entry.segment_id);status='Требует проверки' if entry.needs_review else 'Готово'
            if segment and segment.excluded_from_tts:status='Исключено из озвучки'
            elif segment and not segment.has_current_audio:status='Озвучка не готова'
            elif segment and (entry.audio_revision!=segment.audio_revision):status='Озвучка изменилась'
            values=[entry.segment_id,segment.text if segment else 'Удалённая реплика',f'{entry.audio_duration:.2f} с',f'{entry.video_duration:.2f} с',status]
            for col,value in enumerate(values):self.timeline_table.setItem(row,col,QTableWidgetItem(str(value)))
        self.timeline_status.setText('Монтаж готов к экспорту.' if not issues else f'Нужно исправить: {len(issues)}.')
    def selected_timeline_entry(self):
        if not self.timeline or self.timeline_table.currentRow()<0:return None
        return self.timeline.entries[self.timeline_table.currentRow()]
    def refresh_timeline_selection(self):
        entry=self.selected_timeline_entry()
        self.timeline_fragments.setRowCount(len(entry.fragments) if entry else 0)
        if not entry:return
        for row,fragment in enumerate(entry.fragments):
            for column,value in enumerate([row+1,f'{fragment.start:.3f}',f'{fragment.end:.3f}']):self.timeline_fragments.setItem(row,column,QTableWidgetItem(str(value)))
        if entry.fragments:self.timeline_fragments.selectRow(len(entry.fragments)-1)
    def selected_timeline_fragment_index(self):
        row=self.timeline_fragments.currentRow()
        return row if row>=0 else None
    def refresh_fragment_selection(self):
        entry=self.selected_timeline_entry();index=self.selected_timeline_fragment_index()
        if entry and index is not None and index<len(entry.fragments):
            fragment=entry.fragments[index];self.cut_start.setText(f'{fragment.start:.3f}');self.cut_end.setText(f'{fragment.end:.3f}')
    def timeline_fragment_from_fields(self):
        start=float(self.cut_start.text().replace(',','.'));end=float(self.cut_end.text().replace(',','.'))
        source_index=self.project.video_source.locate(start)[0]
        fragment=VideoFragment(start,end,source_index=source_index)
        if fragment.end>(self.project.video_source.duration+.02):raise TimelineError('Конец фрагмента находится за пределами фильма.')
        return fragment
    def mark_cut_start(self):self.cut_start.setText(f'{self.video_offset+self.video_player.position()/1000:.3f}');self.tabs.setCurrentIndex(3)
    def mark_cut_end(self):self.cut_end.setText(f'{self.video_offset+self.video_player.position()/1000:.3f}');self.tabs.setCurrentIndex(3)
    def timeline_add(self):
        if not self.ensure_timeline():return
        entry=self.selected_timeline_entry()
        try:
            if not entry:raise TimelineError('Выберите строку монтажа.')
            add_fragment(entry,self.timeline_fragment_from_fields());self.save_timeline();self.refresh_timeline()
        except (ValueError,ProjectFormatError,TimelineError) as error:QMessageBox.warning(self,'Монтаж',str(error))
    def timeline_replace(self):
        entry=self.selected_timeline_entry();index=self.selected_timeline_fragment_index()
        try:
            if not entry or index is None:raise TimelineError('Выберите фрагмент монтажа.')
            replace_fragment(entry,index,self.timeline_fragment_from_fields());self.save_timeline();self.refresh_timeline()
        except (ValueError,ProjectFormatError,TimelineError) as error:QMessageBox.warning(self,'Монтаж',str(error))
    def timeline_remove(self):
        entry=self.selected_timeline_entry();index=self.selected_timeline_fragment_index()
        try:
            if not entry or index is None:raise TimelineError('Выберите фрагмент монтажа.')
            remove_fragment(entry,index);self.save_timeline();self.refresh_timeline()
        except TimelineError as error:QMessageBox.warning(self,'Монтаж',str(error))
    def timeline_autofill(self):
        if not self.ensure_timeline():return
        entry=self.selected_timeline_entry()
        if not entry:QMessageBox.information(self,'Монтаж','Выберите строку монтажа.');return
        scenes=[]
        try:
            if self.project.scene_analysis:scenes=read_scenes(self.root/self.project.scene_analysis.scenes_path,self.project.video_source.fingerprint)
            autofill(entry,source_duration=self.project.video_source.duration,scenes=scenes);self.save_timeline();self.refresh_timeline()
        except Exception as error:QMessageBox.warning(self,'Автодобор',str(error))
    def timeline_preview(self):
        entry=self.selected_timeline_entry()
        if not entry or not entry.fragments or not self.project or not self.project.video_source:return
        self.preview_fragments=entry.fragments;self.preview_fragment_index=0;self.play_preview_fragment();self.tabs.setCurrentIndex(1)
    def timeline_play_audio(self):
        entry=self.selected_timeline_entry()
        segment=next((item for item in self.project.segments if entry and item.segment_id==entry.segment_id),None) if self.project else None
        if segment and segment.has_current_audio and (self.root/segment.audio).is_file():self.player.setSource(QUrl.fromLocalFile(str(self.root/segment.audio)));self.player.play()
        else:QMessageBox.information(self,'Озвучка','Для выбранной строки нет актуального аудиофайла.')
    def timeline_confirm(self):
        entry=self.selected_timeline_entry()
        if not entry or not self.project:return
        segment=next((item for item in self.project.segments if item.segment_id==entry.segment_id),None)
        if not segment or not segment.has_current_audio or entry.video_duration+.02<(segment.duration or 0):QMessageBox.warning(self,'Монтаж','Строка ещё не покрывает длительность готовой озвучки.');return
        entry.audio_duration=segment.duration or 0;entry.audio_revision=segment.audio_revision;entry.needs_review=False;entry.reason=None;self.save_timeline();self.refresh_timeline()
    def refresh_export(self):
        if not self.ensure_timeline():self.export_status.setText('Выберите фильм и создайте монтаж.');self.export_button.setEnabled(False);return
        issues=validate_timeline(self.project,self.timeline,self.root);self.export_button.setEnabled(not issues and not self.video_worker)
        self.export_status.setText('Можно экспортировать в export/final.mp4.' if not issues else 'Экспорт заблокирован:\n'+'\n'.join(issues[:5]))
    def start_export(self):
        if self.video_worker or not self.ensure_timeline():return
        issues=validate_timeline(self.project,self.timeline,self.root)
        if issues:QMessageBox.warning(self,'Экспорт','Экспорт заблокирован:\n'+'\n'.join(issues));return
        if not ffprobe_is_available():QMessageBox.warning(self,'Экспорт','FFmpeg не найден.');return
        project,timeline,root=self.project,self.timeline,self.root;resolution=self.export_resolution.currentText();fps=self.export_fps.currentText();source_audio=self.export_audio.currentIndex()==1
        self.start_video_job('Экспортирую ролик…',lambda stopped,report:export(project,timeline,root,resolution=resolution,fps=fps,source_audio=source_audio,cancelled=stopped,progress=lambda text:report(0,0,text)),self.finish_export,self.export_status)
    def finish_export(self,path):self.export_status.setText('Готово: '+str(path));self.statusBar().showMessage('Экспорт завершён.')
    def settings(self): return TTSSettings(self.lang.currentText().strip(),self.voice.currentText().strip(),self.rate.currentText().strip(),self.volume.currentText().strip(),self.pitch.currentText().strip())
    def need(self):
        if self.project:return True
        if QMessageBox.question(self,'Проект','Для этого действия создайте проект. Создать сейчас?',QMessageBox.Yes|QMessageBox.No)==QMessageBox.Yes:self.new(preserve_draft=True)
        return bool(self.project)
    def has_unsaved_draft(self): return bool(self.text.toPlainText().strip()) and (not self.project or self.text.toPlainText()!=self.project.script_text)
    def protect_draft(self):
        if not self.has_unsaved_draft():return True
        dialog=QMessageBox(self);dialog.setWindowTitle('Несохранённый сценарий');dialog.setText('Текст ещё не разбит на сегменты и не сохранён в проекте.')
        save=dialog.addButton('Разбить и сохранить',QMessageBox.AcceptRole);discard=dialog.addButton('Не сохранять',QMessageBox.DestructiveRole);dialog.addButton('Отмена',QMessageBox.RejectRole);dialog.exec()
        if dialog.clickedButton() is save:return self.segment()
        return dialog.clickedButton() is discard
    def new(self,preserve_draft=False):
        had_project=bool(self.project)
        if self.worker or getattr(self,'video_worker',None) or (not preserve_draft and not self.protect_draft()):return
        # Saving a draft without an active project creates that project inside
        # protect_draft(). Do not immediately ask the user to create another.
        if not preserve_draft and not had_project and self.project:return
        draft=self.text.toPlainText() if preserve_draft else ''
        parent=QFileDialog.getExistingDirectory(self,'Папка для нового проекта',str(self.paths.projects));name,ok=QInputDialog.getText(self,'Новый проект','Название:') if parent else ('',False)
        if ok:
            try:self.project,self.root=self.flow.create(Path(parent),name);self.timeline=None
            except WorkflowError as e:QMessageBox.warning(self,'Проект',str(e));return
            self.title.setText('Проект: '+self.project.name);self.apply();self.refresh_video()
            if preserve_draft:self.text.setPlainText(draft);self.text.setReadOnly(False)
    def open(self):
        if self.worker or getattr(self,'video_worker',None):return
        path=QFileDialog.getExistingDirectory(self,'Папка проекта')
        if path and self.protect_draft():self.open_project(Path(path))
    def open_project(self,root,recover_from_backup=False):
        try:project=self.flow.open(root,recover_from_backup=recover_from_backup)
        except WorkflowError as e:
            if self.flow.recovery_available and QMessageBox.question(self,'Восстановление проекта',f'{e}\n\nВосстановить резервную копию и продолжить работу?',QMessageBox.Yes|QMessageBox.No)==QMessageBox.Yes:self.open_project(root,True)
            else:QMessageBox.warning(self,'Открытие',str(e))
        else:
            self.root=root;self.project=project;self.timeline=None;self.preview_fragments=[];self.title.setText('Проект: '+self.project.name);self.apply();self.refresh_video()
            if recover_from_backup:self.statusBar().showMessage('Резервная копия восстановлена; повреждённый файл сохранён отдельно.')
    def folder(self):
        if self.root:__import__('os').startfile(self.root)
    def diagnostics(self):
        from narezchik.services.media import ffprobe_is_available
        model_names=', '.join(path.name for path in self.paths.whisper_models.iterdir() if path.is_dir()) or 'ещё не загружены'
        try:
            import torch
            gpu=torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'недоступна — анализ кадров работает на CPU'
        except ImportError:gpu='модуль анализа кадров не установлен'
        QMessageBox.information(self,'Диагностика',f'Папка данных:\n{self.paths.data}\n\nFFmpeg/ffprobe: {"готов" if ffprobe_is_available() else "не найден"}\nCUDA/GPU: {gpu}\nМодели Whisper: {model_names}\n\nEdge TTS использует интернет.')
    def import_txt(self):
        p,_=QFileDialog.getOpenFileName(self,'TXT',filter='TXT (*.txt)')
        if p and (not self.text.toPlainText().strip() or QMessageBox.question(self,'Импорт','Заменить текущий текст?',QMessageBox.Yes|QMessageBox.No)==QMessageBox.Yes):
            try:self.text.setReadOnly(False);self.text.setPlainText(read_text_file(Path(p))[0])
            except Exception as e:QMessageBox.warning(self,'Импорт',str(e))
    def clear(self):
        if QMessageBox.question(self,'Очистить','Сегменты будут удалены, а MP3 перенесены в технический архив проекта.',QMessageBox.Yes|QMessageBox.No)==QMessageBox.Yes:
            self.text.setReadOnly(False);self.text.clear()
            if self.project:self.flow.clear_script(self.project,self.root);self.refresh()
    def segment(self):
        if not self.need() or not self.apply_tts_settings():return False
        if self.project.segments and QMessageBox.question(self,'Пересегментация','Заменить список? Совпадающее аудио сохранится.',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return False
        self.flow.replace_script(self.project,self.root,self.text.toPlainText(),self.mode.currentData());self.text.setPlainText(self.project.script_text);self.text.setReadOnly(True);self.refresh();return True
    def apply(self):
        p=self.project;self.applying_settings=True
        try:self.lang.setCurrentText(p.tts_settings.language);self.voice.setCurrentText(p.tts_settings.voice);self.rate.setCurrentText(p.tts_settings.rate);self.volume.setCurrentText(p.tts_settings.volume);self.pitch.setCurrentText(p.tts_settings.pitch)
        finally:self.applying_settings=False
        self.text.setPlainText(p.script_text);self.text.setReadOnly(bool(p.segments));self.refresh();self.refresh_matching()
    def apply_tts_settings(self):
        if not self.project:return True
        try:settings=self.settings()
        except Exception as e:QMessageBox.warning(self,'Настройки озвучки',str(e));return False
        if settings==self.project.tts_settings:return True
        if any(s.has_current_audio for s in self.project.segments) and QMessageBox.question(self,'Изменить настройки озвучки','Настройки применятся ко всему проекту. Готовые MP3 останутся на диске, но станут устаревшими. Переозвучить их сейчас не нужно. Продолжить?',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:self.apply();return False
        self.project.set_tts_settings(settings);self.flow.save(self.project,self.root);self.refresh();return True
    def refresh(self):
        s=self.project.segments if self.project else [];self.table.setRowCount(len(s))
        for r,x in enumerate(s):
            status=STATUS_LABELS[x.status]+((': '+x.error) if x.error else '')
            for c,v in enumerate([x.segment_id,x.text,status,'' if x.duration is None else f'{x.duration:.2f} с','Да' if not x.excluded_from_tts else 'Нет']):self.table.setItem(r,c,QTableWidgetItem(str(v)))
    def selected(self):
        if not self.project or self.table.currentRow()<0:return None
        return self.project.segments[self.table.currentRow()]
    def changed(self,before):self.flow.after_segment_change(self.project,self.root,before);self.text.setPlainText(self.project.script_text);self.refresh()
    def edit(self):
        s=self.selected()
        if s:
            t,ok=QInputDialog.getMultiLineText(self,'Правка','Текст:',s.text)
            if ok:
                try:
                    b={x.segment_id:x.audio for x in self.project.segments if x.audio};self.project.edit_segment(s.segment_id,t)
                except ProjectFormatError as e:
                    QMessageBox.warning(self,'Правка сегмента',str(e));return
                self.changed(b)
    def split(self):
        s=self.selected()
        if s:
            t,ok=QInputDialog.getMultiLineText(self,'Разделить','Каждая строка — часть:',s.text);parts=[x.strip() for x in t.splitlines() if x.strip()]
            if ok and len(parts)>1:b={x.segment_id:x.audio for x in self.project.segments if x.audio};self.project.split_segment(s.segment_id,parts);self.changed(b)
    def merge(self):
        s=self.selected()
        if s and s!=self.project.segments[-1]:b={x.segment_id:x.audio for x in self.project.segments if x.audio};self.project.merge_with_next(s.segment_id);self.changed(b)
    def delete(self):
        s=self.selected()
        if s and QMessageBox.question(self,'Удалить','Удалить из сценария? MP3 будет перенесён в технический архив.',QMessageBox.Yes|QMessageBox.No)==QMessageBox.Yes:b={x.segment_id:x.audio for x in self.project.segments if x.audio};self.project.remove_segment(s.segment_id);self.changed(b)
    def exclude(self):
        s=self.selected()
        if s:b={x.segment_id:x.audio for x in self.project.segments if x.audio};self.project.set_segment_excluded(s.segment_id,not s.excluded_from_tts);self.changed(b)
    def play(self):
        s=self.selected()
        if s and s.can_play:self.player.setSource(QUrl.fromLocalFile(str(self.root/s.audio)));self.player.play()
    def voices(self):
        try:
            v=self.tts.list_voices();names=[x['name'] for x in v if x['locale']==self.lang.currentText()];self.applying_settings=True
            try:self.voice.clear();self.voice.addItems(names)
            finally:self.applying_settings=False
            if self.root:(self.root/'voice_cache.json').write_text(json.dumps(v,ensure_ascii=False),encoding='utf8')
        except Exception as e:
            cache=self.root/'voice_cache.json' if self.root else None
            if cache and cache.is_file():
                try:v=json.loads(cache.read_text(encoding='utf8'));self.voice.clear();self.voice.addItems([x['name'] for x in v if x['locale']==self.lang.currentText()]);self.statusBar().showMessage('Использован сохранённый каталог голосов.');return
                except (OSError,json.JSONDecodeError):pass
            QMessageBox.warning(self,'Голоса',f'Нет сети. Имя голоса можно ввести вручную.\n{e}')
    def preview(self):
        if self.need():
            try:p=self.root/'audio'/'test.mp3';self.tts.synthesize(self.sample.toPlainText(),self.settings(),p);self.player.setSource(QUrl.fromLocalFile(str(p)));self.player.play()
            except Exception as e:QMessageBox.warning(self,'Проверка',str(e))
    def generate(self,force):
        if not self.need() or self.worker or self.video_worker or not self.apply_tts_settings():return
        if force and QMessageBox.question(self,'Перегенерация','Заменить все MP3?',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
        if not ffprobe_is_available():QMessageBox.warning(self,'FFmpeg','Не найден ffprobe. Установите полный комплект FFmpeg и добавьте его в PATH, затем перезапустите приложение.');return
        self.flow.save(self.project,self.root);self.thread=QThread(self);self.worker=Worker(TTSQueue(self.tts),self.flow,self.project,self.root,force);self.worker.moveToThread(self.thread);self.thread.started.connect(self.worker.run);self.worker.progress.connect(self.update_tts_progress);self.worker.done.connect(self.finished);self.thread.start();self.cancel.setEnabled(True)
    def update_tts_progress(self,current,total,text):
        self.progress.setMaximum(total);self.progress.setValue(current-1);self.progress.setFormat(f'{current}/{total}: {text[:50]}')
    def regenerate(self):
        s=self.selected()
        if not s or not self.need() or self.worker or self.video_worker or not self.apply_tts_settings():return
        if s.excluded_from_tts:QMessageBox.information(self,'Перегенерация','Сначала включите сегмент в озвучку.');return
        if not ffprobe_is_available():QMessageBox.warning(self,'FFmpeg','Не найден ffprobe. Установите полный комплект FFmpeg и добавьте его в PATH, затем перезапустите приложение.');return
        s.mark_stale();self.project.invalidate_segment_matches({s.segment_id});self.flow.save(self.project,self.root);self.thread=QThread(self);self.worker=Worker(TTSQueue(self.tts),self.flow,self.project,self.root,False,{s.segment_id});self.worker.moveToThread(self.thread);self.thread.started.connect(self.worker.run);self.worker.done.connect(self.finished);self.thread.start();self.cancel.setEnabled(True)
    def finished(self,result):
        self.thread.quit();self.thread.wait();self.worker=None;self.thread=None;self.cancel.setEnabled(False);self.refresh()
        if result.cancelled:self.statusBar().showMessage(f'Озвучка остановлена. Осталось реплик: {result.target_count-result.processed_count}.')
        elif result.target_count:self.statusBar().showMessage('Очередь озвучки завершена.')
        else:self.statusBar().showMessage('Нет реплик, которым требуется озвучка.')
    def closeEvent(self,event):
        if self.worker or self.video_worker:QMessageBox.information(self,'Операция выполняется','Сначала остановите выполняющуюся операцию.');event.ignore()
        elif self.protect_draft():event.accept()
        else:event.ignore()

def main():
    import multiprocessing
    multiprocessing.freeze_support()
    app=QApplication(sys.argv);w=MainWindow();w.show();return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
