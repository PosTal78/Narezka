"""Stage-three selection rules use a synthetic index, never neural models or video."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json
from unittest.mock import patch

from narezchik.main import MainWindow
from narezchik.models import (Project, ProjectFormatError, SegmentMatch, TTSSettings,
                              VideoFragment, VideoIndexState, VideoSource)
from narezchik.services import matching
from narezchik.services.analysis import Scene
from narezchik.services.matching import (PARTIAL_INDEX_FILENAME, IndexedScene, build_index,
                                          confirm_all_matches, read_index, seed_part_cache, select_matches)
from narezchik.services.subtitles import SubtitleCue

FINGERPRINT = "a" * 64

def ready(project: Project, text: str, duration: float = 4) -> int:
    segment = project.add_segment(text)
    segment.mark_ready(duration, TTSSettings())
    return segment.segment_id

class MatchingTests(unittest.TestCase):
    def test_bulk_confirmation_accepts_first_candidate_and_keeps_unresolved(self) -> None:
        matches = [SegmentMatch(1, candidates=[[VideoFragment(1, 2)]], confidence=.4,
                                needs_review=True, reason="Проверить"), SegmentMatch(2)]
        self.assertEqual(confirm_all_matches(matches), (1, 1))
        self.assertEqual(matches[0].fragments, [VideoFragment(1, 2)])
        self.assertEqual(matches[0].confirmation, "bulk")
        self.assertTrue(matches[0].manual)
        self.assertTrue(matches[1].needs_review)
    def setUp(self) -> None:
        self.index = [
            IndexedScene(1, 0, 5, "Маша открывает дверь", "a woman opens a door", ("woman", "door"), None),
            IndexedScene(2, 5, 10, "Погоня начинается", "people run in street", ("people", "street"), None),
            IndexedScene(3, 10, 15, "Маша встречает друга", "two people talk", ("people",), None),
        ]

    def test_low_confidence_is_empty_but_keeps_candidates(self) -> None:
        project = Project("Тест"); identifier = ready(project, "Космический корабль взлетает")
        match = select_matches(project.segments, self.index, threshold=.2)[0]
        self.assertEqual(match.segment_id, identifier); self.assertEqual(match.fragments, [])
        self.assertTrue(match.needs_review); self.assertTrue(match.candidates)

    def test_subtitle_touching_scene_boundary_is_not_duplicated(self) -> None:
        cues = [SubtitleCue(0, 2, "первый"), SubtitleCue(2, 4, "второй")]
        self.assertEqual(matching._cue_text(Scene(1, 0, 2), cues), "первый")
        self.assertEqual(matching._cue_text(Scene(2, 2, 4), cues), "второй")

    def test_ready_segment_uses_several_sequential_fragments_when_needed(self) -> None:
        project = Project("Тест"); ready(project, "Маша открывает дверь", 9)
        match = select_matches(project.segments, self.index, threshold=.01)[0]
        self.assertGreaterEqual(len(match.fragments), 2); self.assertFalse(match.needs_review)

    def test_missing_audio_is_blocked(self) -> None:
        project = Project("Тест"); identifier = project.add_segment("Маша открывает дверь").segment_id
        match = select_matches(project.segments, self.index)[0]
        self.assertEqual(match.segment_id, identifier); self.assertIn("Озвучка", match.reason or "")

    def test_manual_choice_survives_replacement(self) -> None:
        project = Project("Тест"); identifier = ready(project, "Маша открывает дверь")
        project.segment_matches = [SegmentMatch(identifier, [VideoFragment(1, 2)], manual=True, needs_review=False)]
        project.replace_segment_matches(select_matches(project.segments, self.index, threshold=.01))
        self.assertTrue(project.segment_matches[0].manual)

    def test_index_rejects_other_source(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "video_index.json"
            path.write_text('{"source_fingerprint":"' + FINGERPRINT + '","scenes":[]}', encoding="utf-8")
            with self.assertRaises(Exception): read_index(path, "b" * 64)

    def test_index_rejects_other_model_version(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "video_index.json"
            path.write_text('{"source_fingerprint":"' + FINGERPRINT + '","model_version":"old","scenes":[]}', encoding="utf-8")
            with self.assertRaises(Exception): read_index(path, FINGERPRINT)

    def test_index_state_round_trip(self) -> None:
        project = Project("Тест")
        project.video_source = VideoSource(str(Path("C:/film.mp4")), FINGERPRINT, "film.mp4", 1, 1, 1, 1)
        project.set_video_index(VideoIndexState("analysis/video_index.json", FINGERPRINT, "test"))
        self.assertEqual(Project.from_dict(project.to_dict()).video_index.model_version, "test")

    def test_build_index_initializes_default_captioner_once(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); calls = []
            original = matching.local_captioner
            original_extract = matching._extract_thumbnail
            try:
                matching.local_captioner = lambda: (calls.append("loaded") or (lambda _: "a person"))
                matching._extract_thumbnail = lambda _video, output, _at: (output.parent.mkdir(parents=True, exist_ok=True), output.write_bytes(b"frame"))
                build_index(root / "film.mp4", [Scene(1, 0, 1), Scene(2, 1, 2)], [], root / "analysis", FINGERPRINT,
                            embedder=lambda _: (1.0,))
            finally:
                matching.local_captioner = original
                matching._extract_thumbnail = original_extract
            self.assertEqual(calls, ["loaded"])

    def test_script_change_drops_manual_and_orphaned_matches(self) -> None:
        project = Project("Тест"); identifier = ready(project, "Маша открывает дверь")
        project.segment_matches = [SegmentMatch(identifier, [VideoFragment(1, 2)], manual=True, needs_review=False)]
        project.edit_segment(identifier, "Маша закрывает дверь")
        self.assertEqual(project.segment_matches, [])
        project.replace_segment_matches(select_matches(project.segments, self.index, threshold=.01))
        project.remove_segment(identifier)
        self.assertEqual(project.segment_matches, [])

    def test_script_change_keeps_unaffected_manual_match(self) -> None:
        project = Project("Тест"); changed = ready(project, "Маша открывает дверь"); stable = ready(project, "Погоня начинается")
        project.segment_matches = [SegmentMatch(changed, [VideoFragment(1, 2)], manual=True),
                                   SegmentMatch(stable, [VideoFragment(5, 6)], manual=True)]
        project.edit_segment(changed, "Маша закрывает дверь")
        self.assertEqual([item.segment_id for item in project.segment_matches], [stable])

    def test_unready_audio_cannot_keep_manual_match(self) -> None:
        project = Project("Тест"); identifier = ready(project, "Маша открывает дверь")
        project.segment_matches = [SegmentMatch(identifier, [VideoFragment(1, 2)], manual=True, needs_review=False)]
        project.segments[0].mark_stale()
        project.replace_segment_matches(select_matches(project.segments, self.index, threshold=.01))
        self.assertFalse(project.segment_matches[0].manual)
        self.assertIn("Озвучка", project.segment_matches[0].reason or "")

    def test_stale_audio_drops_its_match_before_regeneration_starts(self) -> None:
        project = Project("Тест"); identifier = ready(project, "Маша открывает дверь")
        project.segment_matches = [SegmentMatch(identifier, [VideoFragment(1, 2)], manual=True, needs_review=False)]
        project.segments[0].mark_stale()
        project.invalidate_segment_matches({identifier})
        self.assertEqual(project.segment_matches, [])

    def test_chain_covers_more_than_four_scenes(self) -> None:
        project = Project("Тест"); ready(project, "Маша", 6)
        index = [IndexedScene(number, number - 1, number, "Маша", "Маша", (), None) for number in range(1, 7)]
        match = select_matches(project.segments, index, threshold=.01)[0]
        self.assertEqual(len(match.fragments), 6)
        self.assertFalse(match.needs_review)

    def test_reused_previous_scene_is_a_soft_warning(self) -> None:
        project = Project("Тест"); ready(project, "Маша", 9); ready(project, "Погоня", 5)
        matches = select_matches(project.segments, self.index, threshold=.01)
        self.assertFalse(matches[1].needs_review)
        self.assertIn('возврат', matches[1].reason.lower())

    def test_suspicious_backward_match_is_allowed_with_warning(self) -> None:
        project = Project("Тест"); ready(project, "третья", 5); ready(project, "первая", 5); ready(project, "вторая", 5)
        index = [IndexedScene(1, 0, 5, "первая", "", (), None), IndexedScene(2, 5, 10, "вторая", "", (), None), IndexedScene(3, 10, 15, "третья", "", (), None)]
        matches = select_matches(project.segments, index, threshold=.01)
        self.assertFalse(matches[1].needs_review)
        self.assertIn('возврат', matches[1].reason.lower())
        self.assertFalse(matches[2].needs_review)

    def test_forward_chronology_candidate_below_threshold_requires_review(self) -> None:
        project = Project("Тест"); ready(project, "Маша", 5); ready(project, "Погоня", 5)
        index = [IndexedScene(1, 0, 5, "Маша", "Маша", (), None),
                 IndexedScene(2, 5, 10, "Погоня", "", (), None)]
        matches = select_matches(project.segments, index, threshold=.55,
                                 embedder=lambda text: (1.0,) if text == "Маша" else (-0.16,))
        self.assertEqual(matches[1].fragments, [])
        self.assertTrue(matches[1].needs_review)

    def test_scene_index_keeps_three_moments_per_scene(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_extract = matching._extract_thumbnail
            try:
                matching._extract_thumbnail = lambda _video, output, _at: (output.parent.mkdir(parents=True, exist_ok=True), output.write_bytes(b"frame"))
                path = build_index(root / "film.mp4", [Scene(1, 0, 10)], [SubtitleCue(0, 10, "Действие")], root / "analysis", FINGERPRINT,
                                   captioner=lambda item: item.stem, embedder=lambda _text: (1.0,))
            finally:
                matching._extract_thumbnail = original_extract
            indexed = read_index(path, FINGERPRINT)
            self.assertEqual(len(indexed[0].frame_descriptions), 3)
            self.assertEqual(len(indexed[0].frame_thumbnail_paths), 3)
            self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["complete"])
            with self.assertRaises(Exception):read_index(path,FINGERPRINT,source_duration=5)
    def test_composite_index_uses_local_time_of_each_source_part(self) -> None:
        with TemporaryDirectory() as temporary:
            root=Path(temporary);calls=[];original_extract=matching._extract_thumbnail
            try:
                def extract(video,output,at):
                    calls.append((video.name,round(at,2)));output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(b'frame')
                matching._extract_thumbnail=extract
                path=build_index([(root/'one.mp4',0.0),(root/'two.mp4',10.0)],
                    [Scene(1,0,2,0),Scene(2,10,12,1)],[],root/'analysis',FINGERPRINT,
                    captioner=lambda _:'person',embedder=lambda _:(1.0,))
                indexed=read_index(path,FINGERPRINT)
            finally:matching._extract_thumbnail=original_extract
            self.assertEqual([item.source_index for item in indexed],[0,1])
            self.assertEqual(calls[3][0],'two.mp4');self.assertLess(calls[3][1],1)
    def test_unchanged_source_part_reuses_visual_cache_in_new_composite(self) -> None:
        with TemporaryDirectory() as temporary:
            root=Path(temporary);calls=[];original_extract=matching._extract_thumbnail
            try:
                matching._extract_thumbnail=lambda _video,output,_at:(calls.append(output),output.parent.mkdir(parents=True,exist_ok=True),output.write_bytes(b'frame'))
                build_index([(root/'part.mp4',0.0,'a'*64)],[Scene(1,0,2,0)],[],root/'analysis','b'*64,
                            captioner=lambda _:'person',embedder=lambda _:(1.0,))
                first_count=len(calls)
                completed=build_index([(root/'new.mp4',0.0,'c'*64),(root/'part.mp4',5.0,'a'*64)],
                            [Scene(1,0,5,0),Scene(2,5,7,1)],[],root/'analysis','d'*64,
                            captioner=lambda _:'person',embedder=lambda _:(1.0,))
                indexed=read_index(completed,'d'*64)
            finally:matching._extract_thumbnail=original_extract
            self.assertEqual(len(calls)-first_count,3)
            self.assertEqual(indexed[1].source_index,1)
    def test_existing_single_source_index_can_seed_part_cache_before_replacement(self) -> None:
        with TemporaryDirectory() as temporary:
            root=Path(temporary);analysis=root/'analysis';original_extract=matching._extract_thumbnail;calls=[]
            try:
                matching._extract_thumbnail=lambda _video,output,_at:(calls.append(output),output.parent.mkdir(parents=True,exist_ok=True),output.write_bytes(b'frame'))
                active=build_index(root/'old.mp4',[Scene(1,0,2)],[],analysis,FINGERPRINT,
                                   captioner=lambda _:'person',embedder=lambda _:(1.0,))
                source=VideoSource(str(root/'old.mp4'),FINGERPRINT,'old.mp4',2,100,100,24)
                self.assertEqual(seed_part_cache(root,source,active),1)
                before=len(calls)
                build_index([(root/'old.mp4',5.0,FINGERPRINT)],[Scene(1,5,7,0)],[],analysis,'e'*64,
                            captioner=lambda _:'person',embedder=lambda _:(1.0,))
            finally:matching._extract_thumbnail=original_extract
            self.assertEqual(len(calls),before)

    def test_incomplete_index_is_saved_and_resumed_but_cannot_be_used(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); calls = []
            original_extract = matching._extract_thumbnail
            try:
                matching._extract_thumbnail = lambda _video, output, _at: (output.parent.mkdir(parents=True, exist_ok=True), output.write_bytes(b"frame"))
                with self.assertRaises(Exception):
                    build_index(root / "film.mp4", [Scene(1, 0, 2), Scene(2, 2, 4)], [], root / "analysis", FINGERPRINT,
                                captioner=lambda _item: (calls.append(1) or "frame"), embedder=lambda _text: (1.0,),
                                cancelled=lambda: len(calls) >= 3)
                path = root / "analysis" / PARTIAL_INDEX_FILENAME
                self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["complete"])
                with self.assertRaises(Exception): read_index(path, FINGERPRINT)
                completed = build_index(root / "film.mp4", [Scene(1, 0, 2), Scene(2, 2, 4)], [], root / "analysis", FINGERPRINT,
                                        captioner=lambda _item: "frame", embedder=lambda _text: (1.0,))
                self.assertFalse(path.exists());self.assertEqual(len(read_index(completed, FINGERPRINT)), 2)
            finally:
                matching._extract_thumbnail = original_extract

    def test_cancelled_refresh_does_not_replace_complete_active_index(self) -> None:
        with TemporaryDirectory() as temporary:
            root=Path(temporary);analysis=root/'analysis';original_extract=matching._extract_thumbnail
            try:
                matching._extract_thumbnail=lambda _video,output,_at:(output.parent.mkdir(parents=True,exist_ok=True),output.write_bytes(b'frame'))
                active=build_index(root/'film.mp4',[Scene(1,0,2),Scene(2,2,4)],[],analysis,FINGERPRINT,captioner=lambda _:'old',embedder=lambda _:(1.0,))
                original=active.read_bytes();calls=[]
                with self.assertRaises(Exception):
                    build_index(root/'film.mp4',[Scene(1,0,2),Scene(2,2,4)],[],analysis,FINGERPRINT,
                                captioner=lambda _:(calls.append(1) or 'new'),embedder=lambda _:(1.0,),cancelled=lambda:len(calls)>=3)
                self.assertEqual(active.read_bytes(),original)
                self.assertFalse(json.loads((analysis/PARTIAL_INDEX_FILENAME).read_text(encoding='utf-8'))['complete'])
            finally:matching._extract_thumbnail=original_extract

    def test_cancelled_refresh_cannot_replace_active_keyframes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); analysis = root / "analysis"
            original_extract = matching._extract_thumbnail
            try:
                matching._extract_thumbnail = lambda _video, output, at: (
                    output.parent.mkdir(parents=True, exist_ok=True),
                    output.write_text(f"{at:.3f}", encoding="utf-8"),
                )
                active = build_index(root / "film.mp4", [Scene(1, 0, 3)], [], analysis, FINGERPRINT,
                                     captioner=lambda _: "old", embedder=lambda _: (1.0,))
                active_scene = read_index(active, FINGERPRINT)[0]
                active_frame = root / active_scene.frame_thumbnail_paths[1]
                before = active_frame.read_text(encoding="utf-8")
                calls = []
                with self.assertRaises(Exception):
                    build_index(root / "film.mp4", [Scene(1, 1, 4)], [], analysis, FINGERPRINT,
                                captioner=lambda _: (calls.append(1) or "new"), embedder=lambda _: (1.0,),
                                cancelled=lambda: bool(calls))
                self.assertEqual(active_frame.read_text(encoding="utf-8"), before)
                partial_payload = json.loads((analysis / PARTIAL_INDEX_FILENAME).read_text(encoding="utf-8"))
                self.assertNotEqual(partial_payload["revision"], active.stem.removeprefix("video_index-"))
            finally:
                matching._extract_thumbnail = original_extract

    def test_unactivated_revision_cleanup_removes_only_its_generated_assets(self) -> None:
        with TemporaryDirectory() as temporary:
            analysis = Path(temporary) / "analysis"; revision = "1" * 32
            frames = analysis / "thumbnails" / revision; frames.mkdir(parents=True)
            index_path = analysis / f"video_index-{revision}.json"; index_path.write_text("{}", encoding="utf-8")
            for number in range(1, 4):
                (frames / f"scene-00001-{number}.jpg").write_bytes(b"frame")
            matching.discard_index_revision(index_path)
            self.assertFalse(index_path.exists()); self.assertFalse(frames.exists())

    def test_resume_rebuilds_scene_when_subtitles_changed(self) -> None:
        with TemporaryDirectory() as temporary:
            root=Path(temporary);analysis=root/'analysis';calls=[];original_extract=matching._extract_thumbnail
            scenes=[Scene(1,0,2),Scene(2,2,4)]
            try:
                matching._extract_thumbnail=lambda _video,output,_at:(output.parent.mkdir(parents=True,exist_ok=True),output.write_bytes(b'frame'))
                with self.assertRaises(Exception):
                    build_index(root/'film.mp4',scenes,[SubtitleCue(0,2,'старый текст')],analysis,FINGERPRINT,
                                captioner=lambda _:(calls.append(1) or 'frame'),embedder=lambda _:(1.0,),cancelled=lambda:len(calls)>=3)
                calls.clear()
                completed=build_index(root/'film.mp4',scenes,[SubtitleCue(0,2,'новый текст')],analysis,FINGERPRINT,
                                      captioner=lambda _:(calls.append(1) or 'frame'),embedder=lambda _:(1.0,))
                self.assertEqual(len(calls),6);self.assertEqual(read_index(completed,FINGERPRINT)[0].subtitle_text,'новый текст')
            finally:matching._extract_thumbnail=original_extract

    def test_complete_index_requires_three_existing_frames_and_finite_embeddings(self) -> None:
        with TemporaryDirectory() as temporary:
            root=Path(temporary);analysis=root/'analysis';analysis.mkdir()
            payload={'source_fingerprint':FINGERPRINT,'model_version':matching.INDEX_MODEL_VERSION,'complete':True,
                     'scenes':[{'scene_id':1,'start':0,'end':1,'subtitle_text':'текст','description':'кадр','entities':[],
                                'thumbnail_path':'analysis/missing.jpg','embedding':[float('nan')],
                                'frame_descriptions':['a','b','c'],'frame_embeddings':[[1],[1],[1]],
                                'frame_thumbnail_paths':['analysis/a.jpg','analysis/b.jpg','analysis/c.jpg']}]}
            path=analysis/'bad.json';path.write_text(json.dumps(payload),encoding='utf-8')
            with self.assertRaises(Exception):read_index(path,FINGERPRINT)

    def test_complete_index_rejects_coerced_boolean_scene_fields(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); analysis = root / "analysis"; frames = analysis / "frames"; frames.mkdir(parents=True)
            paths = []
            for number in range(3):
                frame = frames / f"{number}.jpg"; frame.write_bytes(b"frame")
                paths.append(str(frame.relative_to(root)).replace("\\", "/"))
            payload = {"source_fingerprint": FINGERPRINT, "model_version": matching.INDEX_MODEL_VERSION,
                       "complete": True, "scenes": [{"scene_id": True, "start": 0, "end": 1,
                       "subtitle_text": "текст", "description": "кадр", "entities": [],
                       "thumbnail_path": paths[1], "embedding": [1], "frame_descriptions": ["a", "b", "c"],
                       "frame_embeddings": [[1], [1], [1]], "frame_thumbnail_paths": paths}]}
            path = analysis / "bad-types.json"; path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(Exception):
                read_index(path, FINGERPRINT)

    def test_match_state_rejects_string_boolean_flags(self) -> None:
        data = SegmentMatch(1).to_dict(); data["manual"] = "false"
        with self.assertRaises(ProjectFormatError):
            SegmentMatch.from_dict(data)

    def test_malformed_match_collection_is_reported_as_project_format_error(self) -> None:
        project = Project("Тест").to_dict()
        project["segment_matches"] = [{"segment_id": 1, "candidates": None}]
        with self.assertRaises(ProjectFormatError):
            Project.from_dict(project)

    def test_video_fragment_rejects_zero_scene_id(self) -> None:
        with self.assertRaises(ProjectFormatError):VideoFragment(0,1,0)

    def test_missing_subtitle_confirmation_never_becomes_ready(self) -> None:
        project = Project("Тест"); ready(project, "Маша открывает дверь")
        index = [IndexedScene(1, 0, 5, "", "Маша открывает дверь", (), None)]
        match = select_matches(project.segments, index, threshold=.01)[0]
        self.assertTrue(match.needs_review); self.assertEqual(match.fragments, [])
        self.assertIn("субтитров", match.reason or "")

    def test_rejected_match_does_not_advance_chronology(self) -> None:
        project=Project('Тест');ready(project,'визуал',5);ready(project,'ранняя сцена',5)
        index=[IndexedScene(1,0,5,'ранняя сцена','ранняя сцена',(),None),
               IndexedScene(2,10,15,'','визуал',(),None)]
        matches=select_matches(project.segments,index,threshold=.01)
        self.assertTrue(matches[0].needs_review);self.assertFalse(matches[1].needs_review)

    def test_every_automatic_fragment_must_be_relevant(self) -> None:
        project = Project("Тест"); ready(project, "Маша открывает дверь", 9)
        index = [IndexedScene(1, 0, 5, "Маша открывает дверь", "", (), None),
                 IndexedScene(2, 5, 10, "Погоня", "", (), None)]
        match = select_matches(project.segments, index, threshold=.2)[0]
        self.assertTrue(match.needs_review); self.assertEqual(match.fragments, [])
        self.assertIn("длительности", match.reason or "")

    def test_manual_context_is_local_and_visible_in_match_state(self) -> None:
        project = Project("Тест"); first=ready(project, "Маша открывает дверь");ready(project,"Погоня начинается")
        manual = SegmentMatch(first, [VideoFragment(0, 1)], manual=True, needs_review=False)
        matches = select_matches(project.segments, self.index, threshold=.01, manual_matches=[manual])
        self.assertFalse(matches[0].context_used);self.assertTrue(matches[1].context_used)

    def test_future_manual_choice_does_not_influence_earlier_segment(self) -> None:
        project=Project('Тест');ready(project,'Маша открывает дверь');future=ready(project,'Погоня начинается')
        manual=SegmentMatch(future,[VideoFragment(10,11)],manual=True,needs_review=False)
        matches=select_matches(project.segments,self.index,threshold=.01,manual_matches=[manual])
        self.assertFalse(matches[0].context_used)

    def test_soft_manual_context_cannot_approve_below_threshold_scene(self) -> None:
        project=Project('Тест');first=ready(project,'ручной выбор',1);ready(project,'совсем другой текст',1)
        manual=SegmentMatch(first,[VideoFragment(0,1)],manual=True,needs_review=False)
        index=[IndexedScene(1,5,6,'неподходящие субтитры','чужой кадр',(),None,(0.5,),(),((0.5,),),())]
        matches=select_matches(project.segments,index,threshold=.55,embedder=lambda _:(1.0,),manual_matches=[manual])
        self.assertTrue(matches[1].context_used);self.assertTrue(matches[1].needs_review);self.assertEqual(matches[1].fragments,[])

    def test_failed_manual_save_restores_previous_choice(self) -> None:
        match=SegmentMatch(1,[],[[VideoFragment(1,2)]],confidence=.2,needs_review=True,reason='Проверить')
        class Flow:
            def save(self,*_args):raise OSError('disk full')
        class Window:
            project=Project('Тест');root=Path('.');flow=Flow()
            def selected_match(self):return match
            def match_fragments(self,_match):return _match.candidates[0]
            def refresh_matching(self):raise AssertionError('must not refresh after failed save')
        with patch('narezchik.main.QMessageBox.warning'):
            MainWindow.choose_match(Window())
        self.assertEqual(match.fragments,[]);self.assertFalse(match.manual);self.assertTrue(match.needs_review);self.assertEqual(match.reason,'Проверить')
