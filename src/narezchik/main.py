"""Stage-one desktop application."""
from __future__ import annotations
import json, sys, threading
from pathlib import Path
from PySide6.QtCore import QObject, QThread, Signal, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import QApplication, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMessageBox, QPushButton, QPlainTextEdit, QProgressBar, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget
from narezchik.core import ProjectWorkflow, WorkflowError
from narezchik.models import ProjectFormatError, SegmentStatus, TTSSettings
from narezchik.services import SegmentationMode, ffprobe_is_available, read_text_file
from narezchik.services.tts import EdgeTTSService, TTSQueue

STATUS_LABELS = {SegmentStatus.READY: 'Готово', SegmentStatus.NEEDS_TTS: 'Нужно озвучить', SegmentStatus.STALE: 'Нужно переозвучить', SegmentStatus.EXCLUDED: 'Исключено', SegmentStatus.FILE_MISSING: 'Аудиофайл не найден', SegmentStatus.ERROR: 'Ошибка'}

class Worker(QObject):
    progress=Signal(int,int,str); done=Signal(object)
    def __init__(self, q, flow, project, root, force, only_ids=None): super().__init__(); self.q,self.flow,self.project,self.root,self.force,self.only_ids=q,flow,project,root,force,only_ids; self.stop=threading.Event()
    def run(self):
        result=self.q.generate(self.project,self.root,force=self.force,only_ids=self.only_ids,cancelled=self.stop.is_set,progress=lambda a,b,c:self.progress.emit(a,b,c),saved=lambda:self.flow.save(self.project,self.root)); self.done.emit(result)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__(); self.flow=ProjectWorkflow(); self.project=None; self.root=None; self.tts=EdgeTTSService(); self.worker=None; self.thread=None; self.applying_settings=False
        self.player=QMediaPlayer(self); self.player.setAudioOutput(QAudioOutput(self)); self.setWindowTitle('Narezchik'); self.resize(1050,720); self.build()
    def build(self):
        root=QWidget(); box=QVBoxLayout(root); top=QHBoxLayout(); self.title=QLabel('Проект не выбран — несохранённый черновик'); top.addWidget(self.title,1)
        for label,fn in [('Новый проект',self.new),('Выбрать проект',self.open),('Открыть папку',self.folder)]: b=QPushButton(label);b.clicked.connect(fn);top.addWidget(b)
        box.addLayout(top); tabs=QTabWidget(); tabs.addTab(self.voice_tab(),'Озвучка')
        for name in ('Видео','Подбор кадров','Монтаж','Экспорт'): tabs.addTab(QLabel('Будет доступно после этапа озвучки.'),name)
        box.addWidget(tabs); self.setCentralWidget(root)
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
        if self.worker or (not preserve_draft and not self.protect_draft()):return
        # Saving a draft without an active project creates that project inside
        # protect_draft(). Do not immediately ask the user to create another.
        if not preserve_draft and not had_project and self.project:return
        draft=self.text.toPlainText() if preserve_draft else ''
        parent=QFileDialog.getExistingDirectory(self,'Папка для нового проекта');name,ok=QInputDialog.getText(self,'Новый проект','Название:') if parent else ('',False)
        if ok:
            try:self.project,self.root=self.flow.create(Path(parent),name)
            except WorkflowError as e:QMessageBox.warning(self,'Проект',str(e));return
            self.title.setText('Проект: '+self.project.name);self.apply()
            if preserve_draft:self.text.setPlainText(draft);self.text.setReadOnly(False)
    def open(self):
        if self.worker:return
        path=QFileDialog.getExistingDirectory(self,'Папка проекта')
        if path and self.protect_draft():self.open_project(Path(path))
    def open_project(self,root,recover_from_backup=False):
        try:project=self.flow.open(root,recover_from_backup=recover_from_backup)
        except WorkflowError as e:
            if self.flow.recovery_available and QMessageBox.question(self,'Восстановление проекта',f'{e}\n\nВосстановить резервную копию и продолжить работу?',QMessageBox.Yes|QMessageBox.No)==QMessageBox.Yes:self.open_project(root,True)
            else:QMessageBox.warning(self,'Открытие',str(e))
        else:
            self.root=root;self.project=project;self.title.setText('Проект: '+self.project.name);self.apply()
            if recover_from_backup:self.statusBar().showMessage('Резервная копия восстановлена; повреждённый файл сохранён отдельно.')
    def folder(self):
        if self.root:__import__('os').startfile(self.root)
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
        self.text.setPlainText(p.script_text);self.text.setReadOnly(bool(p.segments));self.refresh()
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
        if not self.need() or self.worker or not self.apply_tts_settings():return
        if force and QMessageBox.question(self,'Перегенерация','Заменить все MP3?',QMessageBox.Yes|QMessageBox.No)!=QMessageBox.Yes:return
        if not ffprobe_is_available():QMessageBox.warning(self,'FFmpeg','Не найден ffprobe. Установите полный комплект FFmpeg и добавьте его в PATH, затем перезапустите приложение.');return
        self.flow.save(self.project,self.root);self.thread=QThread(self);self.worker=Worker(TTSQueue(self.tts),self.flow,self.project,self.root,force);self.worker.moveToThread(self.thread);self.thread.started.connect(self.worker.run);self.worker.progress.connect(lambda a,b,t:(self.progress.setMaximum(b),self.progress.setValue(a-1),self.progress.setFormat(f'{a}/{b}: {t[:50]}')));self.worker.done.connect(self.finished);self.thread.start();self.cancel.setEnabled(True)
    def regenerate(self):
        s=self.selected()
        if not s or not self.need() or self.worker or not self.apply_tts_settings():return
        if s.excluded_from_tts:QMessageBox.information(self,'Перегенерация','Сначала включите сегмент в озвучку.');return
        if not ffprobe_is_available():QMessageBox.warning(self,'FFmpeg','Не найден ffprobe. Установите полный комплект FFmpeg и добавьте его в PATH, затем перезапустите приложение.');return
        s.mark_stale();self.flow.save(self.project,self.root);self.thread=QThread(self);self.worker=Worker(TTSQueue(self.tts),self.flow,self.project,self.root,False,{s.segment_id});self.worker.moveToThread(self.thread);self.thread.started.connect(self.worker.run);self.worker.done.connect(self.finished);self.thread.start();self.cancel.setEnabled(True)
    def finished(self,result):
        self.thread.quit();self.thread.wait();self.worker=None;self.thread=None;self.cancel.setEnabled(False);self.refresh()
        if result.cancelled:self.statusBar().showMessage(f'Озвучка остановлена. Осталось реплик: {result.target_count-result.processed_count}.')
        elif result.target_count:self.statusBar().showMessage('Очередь озвучки завершена.')
        else:self.statusBar().showMessage('Нет реплик, которым требуется озвучка.')
    def closeEvent(self,event):
        if self.worker:QMessageBox.information(self,'Озвучка выполняется','Сначала остановите очередь после текущей реплики.');event.ignore()
        elif self.protect_draft():event.accept()
        else:event.ignore()

def main():
    app=QApplication(sys.argv);w=MainWindow();w.show();return app.exec()
