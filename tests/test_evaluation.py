from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from narezchik.models import SegmentMatch, VideoFragment
from narezchik.services.evaluation import (evaluation_ids, evaluation_report, make_label,
                                            read_evaluation, save_evaluation)


class EvaluationTests(unittest.TestCase):
    def test_sample_is_deterministic_and_limited_to_twenty(self) -> None:
        matches=[SegmentMatch(number,confidence=number/100) for number in range(1,46)]
        first=evaluation_ids(matches);second=evaluation_ids(reversed(matches))
        self.assertEqual(first,second);self.assertEqual(len(first),20);self.assertEqual(len(set(first)),20)

    def test_labels_round_trip_without_changing_matches(self) -> None:
        with TemporaryDirectory() as temporary:
            path=Path(temporary)/'evaluation.json';fragment=VideoFragment(1,2,1)
            labels={1:make_label(1,'correct',1,[fragment]),2:make_label(2,'no_acceptable_candidate')}
            save_evaluation(path,labels)
            self.assertEqual(read_evaluation(path),labels)

    def test_report_flags_strong_but_wrong(self) -> None:
        matches=[SegmentMatch(1,[VideoFragment(1,2,1)],needs_review=False),SegmentMatch(2,needs_review=True)]
        labels={1:make_label(1,'no_acceptable_candidate'),2:make_label(2,'correct',0,[VideoFragment(2,3,2)])}
        report=evaluation_report(matches,labels)
        self.assertEqual(report['strong_but_wrong'],1);self.assertEqual(report['top1_correct'],1)


if __name__ == '__main__':
    unittest.main()
