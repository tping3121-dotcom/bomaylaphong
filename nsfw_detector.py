import os
import logging

import cv2
from PIL import Image
from transformers import pipeline


logger = logging.getLogger(__name__)


class NSFWDetector:

    def __init__(self):

        model_name = os.getenv(
            "NSFW_MODEL",
            "Falconsai/nsfw_image_detection"
        )

        logger.info(
            "Loading model: %s",
            model_name
        )

        self.classifier = pipeline(
            "image-classification",
            model=model_name
        )

        logger.info(
            "NSFW model loaded!"
        )

    def check_image(
        self,
        image_path,
        threshold=0.75
    ):

        image = Image.open(
            image_path
        ).convert("RGB")

        results = self.classifier(
            image
        )

        return self._parse_result(
            results,
            threshold
        )

    def check_video(
        self,
        video_path,
        threshold=0.75
    ):

        cap = cv2.VideoCapture(
            video_path
        )

        if not cap.isOpened():

            return {
                "nsfw": False,
                "label": "video_error",
                "score": 0.0
            }

        frame_count = int(
            cap.get(
                cv2.CAP_PROP_FRAME_COUNT
            )
        )

        fps = cap.get(
            cv2.CAP_PROP_FPS
        )

        if fps <= 0:
            fps = 25

        if frame_count <= 0:

            cap.release()

            return {
                "nsfw": False,
                "label": "unknown",
                "score": 0.0
            }

        sample_count = min(
            12,
            max(3, int(frame_count / fps / 2))
        )

        highest_score = 0.0
        highest_label = "unknown"

        for i in range(sample_count):

            position = int(
                i * frame_count /
                sample_count
            )

            cap.set(
                cv2.CAP_PROP_POS_FRAMES,
                position
            )

            success, frame = cap.read()

            if not success:
                continue

            frame = cv2.cvtColor(
                frame,
                cv2.COLOR_BGR2RGB
            )

            image = Image.fromarray(
                frame
            )

            results = self.classifier(
                image
            )

            parsed = self._parse_result(
                results,
                threshold
            )

            if parsed["score"] > highest_score:

                highest_score = parsed["score"]
                highest_label = parsed["label"]

            if parsed["nsfw"]:

                cap.release()

                return parsed

        cap.release()

        return {
            "nsfw": highest_score >= threshold,
            "label": highest_label,
            "score": highest_score
        }

    def _parse_result(
        self,
        results,
        threshold
    ):

        if not results:

            return {
                "nsfw": False,
                "label": "unknown",
                "score": 0.0
            }

        best = max(
            results,
            key=lambda x: x["score"]
        )

        label = str(
            best["label"]
        ).lower()

        score = float(
            best["score"]
        )

        nsfw_keywords = [
            "nsfw",
            "porn",
            "sexual",
            "explicit",
            "hentai",
            "nude",
            "nudity",
            "erotic"
        ]

        is_nsfw = any(
            word in label
            for word in nsfw_keywords
        )

        return {
            "nsfw": (
                is_nsfw
                and score >= threshold
            ),
            "label": label,
            "score": score
        }


detector = NSFWDetector()
