"""Pure stage-four montage and FFmpeg-command tests."""
from __future__ import annotations
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from narezchik.models import Project, ProjectFormatError, SegmentMatch, TTSSettings, VideoFragment, VideoSource
from narezchik.services.timeline import (TimelineEntry, TimelineError, add_fragment, autofill,
                                         create_from_matches, load, remove_fragment,
                                         replace_fragment, save, synchronize, validate)
from narezchik.core import ProjectWorkflow
from narezchik.main import MainWindow
import importlib
export_service = importlib.import_module("narezchik.services.export")

FINGERPRINT = "c" * 64

class Scene:
    def __init__(self, identifier, start, end): self.scene_id,self.start,self.end=identifier,start,end

class TimelineTests(unittest.TestCase):
    def project(self):
        project=Project("Монтаж"); project.video_source=VideoSource(str(Path("C:/movie.mp4")),FINGERPRINT,"movie.mp4",20,1920,1080,24)
        segment=project.add_segment("Реплика");segment.mark_ready(5,TTSSettings());return project,segment
    def test_overflow_trims_only_last_fragment(self):
        entry=TimelineEntry(1,[VideoFragment(0,3),VideoFragment(5,9)],5,1,False)
        from narezchik.services.timeline import trim_to_audio
        trim_to_audio(entry);self.assertEqual([(x.start,x.end) for x in entry.fragments],[(0,3),(5,7)])
    def test_autofill_uses_following_scenes(self):
        entry=TimelineEntry(1,[VideoFragment(0,2)],5,1)
        self.assertTrue(autofill(entry,source_duration=20,scenes=[Scene(1,0,3),Scene(2,3,8)]));self.assertAlmostEqual(entry.video_duration,5)
    def test_changed_audio_blocks_export_validation(self):
        project,segment=self.project(); timeline=create_from_matches(project); entry=timeline.entries[0];add_fragment(entry,VideoFragment(0,5));entry.needs_review=False
        segment.mark_ready(5,TTSSettings());self.assertTrue(validate(project,timeline,Path(".")))
    def test_overlong_video_blocks_export_validation(self):
        project,segment=self.project(); timeline=create_from_matches(project); entry=timeline.entries[0]
        entry.fragments=[VideoFragment(0,6)];entry.needs_review=False
        with TemporaryDirectory() as temporary:
            root=Path(temporary); source=root/'movie.mp4';source.write_bytes(b'x');(root/'audio').mkdir();(root/segment.audio).write_bytes(b'x')
            project.video_source=VideoSource(str(source),FINGERPRINT,'movie.mp4',20,1920,1080,24)
            self.assertIn('кадры не готовы',validate(project,timeline,root)[0])
    def test_save_and_reload(self):
        project,_=self.project();timeline=create_from_matches(project)
        with TemporaryDirectory() as temporary:
            root=Path(temporary);save(root,timeline);self.assertEqual(load(root,FINGERPRINT).source_fingerprint,FINGERPRINT)
    def test_stale_narration_blocks_export_instead_of_reusing_old_audio(self):
        project,segment=self.project();timeline=create_from_matches(project);add_fragment(timeline.entries[0],VideoFragment(0,5))
        segment.edit_text('Изменённая реплика')
        with TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'movie.mp4';source.write_bytes(b'x');project.video_source=VideoSource(str(source),FINGERPRINT,'movie.mp4',20,1920,1080,24)
            self.assertTrue(any('озвучка не готова' in issue for issue in validate(project,timeline,root)))
    def test_timeline_rejects_non_finite_values(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'timeline';path.mkdir();(path/'timeline.json').write_text('{"source_fingerprint":"'+FINGERPRINT+'","entries":[{"segment_id":1,"audio_duration":NaN,"audio_revision":1,"needs_review":false}]}',encoding='utf-8')
            with self.assertRaises(TimelineError):load(root,FINGERPRINT)
        with self.assertRaises(ProjectFormatError):VideoFragment(float('nan'),1)
    def test_editing_timeline_archives_previous_export(self):
        project,_=self.project();timeline=create_from_matches(project)
        with TemporaryDirectory() as temporary:
            root=Path(temporary);final=root/'export'/'final.mp4';final.parent.mkdir();final.write_bytes(b'old')
            save(root,timeline)
            self.assertFalse(final.exists());self.assertEqual([path.read_bytes() for path in (root/'export'/'retired').glob('*.mp4')],[b'old'])
    def test_selected_fragment_can_be_replaced_and_removed(self):
        entry=TimelineEntry(1,[VideoFragment(0,2),VideoFragment(3,6)],5,1,False)
        replace_fragment(entry,0,VideoFragment(1,2.5));self.assertEqual((entry.fragments[0].start,entry.fragments[0].end),(1,2.5))
        remove_fragment(entry,0);self.assertEqual([(item.start,item.end) for item in entry.fragments],[(3,6)])
    def test_synchronize_drops_deleted_rows_but_preserves_current_edits(self):
        project,segment=self.project();timeline=create_from_matches(project);add_fragment(timeline.entries[0],VideoFragment(0,5))
        preserved=timeline.entries[0];project.remove_segment(segment.segment_id);replacement=project.add_segment('Новая');replacement.mark_ready(2,TTSSettings())
        self.assertTrue(synchronize(project,timeline));self.assertNotIn(preserved,timeline.entries);self.assertEqual([entry.segment_id for entry in timeline.entries],[replacement.segment_id])
    def test_synchronize_fills_old_empty_row_but_preserves_user_edit(self):
        project,segment=self.project();project.segment_matches=[SegmentMatch(
                segment.segment_id,[VideoFragment(2,7)],needs_review=False)]
        timeline=create_from_matches(project);entry=timeline.entries[0];entry.fragments=[];entry.needs_review=True
        self.assertTrue(synchronize(project,timeline));self.assertEqual(entry.fragments,[VideoFragment(2,7)])
        entry.fragments=[];entry.edited_by_user=True
        self.assertFalse(synchronize(project,timeline));self.assertEqual(entry.fragments,[])
    def test_export_command_reads_fragments_across_two_source_parts(self):
        project,segment=self.project()
        first=VideoSource(str(Path('C:/one.mp4')),'a'*64,'one.mp4',10,1920,1080,24)
        second=VideoSource(str(Path('C:/two.mp4')),'b'*64,'two.mp4',10,1920,1080,24)
        project.video_source=VideoSource.combine([first,second]);segment.duration=4
        timeline=create_from_matches(project);timeline.entries[0].fragments=[VideoFragment(8,12)];timeline.entries[0].needs_review=False
        with TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'audio').mkdir();(root/segment.audio).write_bytes(b'x');output=root/'out.mp4'
            command=export_service.build_export_command(project,timeline,root,output)
            script=output.with_suffix('.filters.txt').read_text(encoding='utf-8')
            self.assertEqual(command.count('-i'),3)
            self.assertIn('[0:v]trim=start=8.000000:end=10.000000',script)
            self.assertIn('[1:v]trim=start=0.000000:end=2.000000',script)
    def test_opening_another_project_cannot_reuse_previous_timeline(self):
        project,_=self.project()
        class Flow:
            recovery_available=False
            def open(self,_root,recover_from_backup=False):return project
        class Label:
            def setText(self,_text):pass
        class Window:
            flow=Flow();timeline=object();preview_fragments=[object()];title=Label()
            def apply(self):pass
            def refresh_video(self):pass
        window=Window();MainWindow.open_project(window,Path('another-project'))
        self.assertIsNone(window.timeline);self.assertEqual(window.preview_fragments,[])
    def test_replacing_source_archives_timeline_and_export(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary); flow=ProjectWorkflow(); project,created=flow.create(root,'Монтаж')
            old=root/'old.mp4';new=root/'new.mp4';old.write_bytes(b'x');new.write_bytes(b'x')
            old_source=VideoSource(str(old),FINGERPRINT,'old.mp4',20,1920,1080,24)
            new_source=VideoSource(str(new),'d'*64,'new.mp4',20,1920,1080,24)
            flow.replace_video_source(project,created,old_source,archive_previous=False)
            (created/'timeline'/'timeline.json').write_text('{}',encoding='utf-8');(created/'export'/'final.mp4').write_bytes(b'x')
            flow.replace_video_source(project,created,new_source,archive_previous=True)
            archived=list((created/'timeline'/'retired').rglob('*'))
            self.assertTrue(any(path.name=='timeline.json' for path in archived))
            self.assertTrue(any(path.name=='final.mp4' for path in archived))
    def test_export_command_uses_filter_script_and_requested_size(self):
        project,segment=self.project();timeline=create_from_matches(project);entry=timeline.entries[0];add_fragment(entry,VideoFragment(0,5));entry.needs_review=False
        with TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'audio').mkdir();(root/segment.audio).write_bytes(b'x');temporary_output=root/'export'/'temporary.mp4';temporary_output.parent.mkdir()
            original=export_service.media_tool
            try:
                export_service.media_tool=lambda _:'ffmpeg'
                command=export_service.build_export_command(project,timeline,root,temporary_output,resolution='720p',fps='30')
                script=temporary_output.with_suffix('.filters.txt').read_text(encoding='utf-8')
            finally: export_service.media_tool=original
            self.assertIn('-filter_complex_script',command);self.assertIn('scale=1280:720',script);self.assertIn('fps=30.000000',script)
            self.assertEqual(script.count('fps='),1);self.assertIn('trim=duration=5.000000',script)
    def test_excluded_timeline_row_cannot_leak_old_narration_into_export(self):
        project,first=self.project();second=project.add_segment('Заголовок');second.mark_ready(2,TTSSettings());timeline=create_from_matches(project)
        add_fragment(timeline.entries[0],VideoFragment(0,5));add_fragment(timeline.entries[1],VideoFragment(5,7));project.set_segment_excluded(second.segment_id,True)
        with TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'audio').mkdir();(root/first.audio).write_bytes(b'x');temporary_output=root/'export'/'temporary.mp4';temporary_output.parent.mkdir()
            original=export_service.media_tool
            try:
                export_service.media_tool=lambda _:'ffmpeg';command=export_service.build_export_command(project,timeline,root,temporary_output);script=temporary_output.with_suffix('.filters.txt').read_text(encoding='utf-8')
            finally:export_service.media_tool=original
            self.assertNotIn(f'n{second.segment_id}',script);self.assertEqual(command.count('-i'),2)
    def test_export_rechecks_source_contents_before_running_ffmpeg(self):
        project,segment=self.project();timeline=create_from_matches(project);add_fragment(timeline.entries[0],VideoFragment(0,5));timeline.entries[0].needs_review=False
        with TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'movie.mp4';source.write_bytes(b'not-the-imported-file');project.video_source=VideoSource(str(source),FINGERPRINT,'movie.mp4',20,1920,1080,24)
            (root/'audio').mkdir();(root/segment.audio).write_bytes(b'x')
            with self.assertRaisesRegex(export_service.ExportError,'изменилось'):
                export_service.export(project,timeline,root)
    def test_changed_audio_file_duration_blocks_export(self):
        project,segment=self.project()
        with patch.object(export_service,'probe_duration',return_value=4.0):
            with self.assertRaisesRegex(export_service.ExportError,'аудиофайл изменился'):
                export_service._verify_audio_durations(project,Path('.'))
