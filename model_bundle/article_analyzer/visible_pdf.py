"""Keep geometry used for table extraction inside PDF page and Form XObject bounds."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property

from pdfminer.layout import LTContainer, LTCurve, LTFigure, LTLine
from pdfminer.pdfinterp import PDFPageInterpreter
from pdfminer.utils import MATRIX_IDENTITY, Matrix, Point, Rect, apply_matrix_rect
from pdfplumber.page import PDFPageAggregatorWithMarkedContent, Page


@dataclass(frozen=True)
class Clip:
    bbox: Rect
    matrix: Matrix

    @cached_property
    def inverse(self):
        a, b, c, d, e, f = self.matrix
        determinant = a * d - b * c
        if not determinant:
            return None
        return (d / determinant, -b / determinant, -c / determinant, a / determinant,
                (c * f - d * e) / determinant, (b * e - a * f) / determinant)

    def local(self, point: Point) -> Point:
        a, b, c, d, e, f = self.inverse
        x, y = point
        return a * x + c * y + e, b * x + d * y + f

    def contains(self, point: Point):
        if self.inverse is None:
            return False
        x, y = self.local(point)
        left, lower, right, upper = self.bbox
        tolerance = 1e-6
        return left - tolerance <= x <= right + tolerance and lower - tolerance <= y <= upper + tolerance

    def segment(self, start: Point, stop: Point):
        if self.inverse is None:
            return None
        lower, upper = 0., 1.
        local_start, local_stop = self.local(start), self.local(stop)
        for origin, destination, lo, hi in zip(local_start, local_stop, self.bbox[:2], self.bbox[2:]):
            delta = destination - origin
            if not delta:
                if not lo <= origin <= hi:
                    return None
                continue
            a, b = sorted(((lo - origin) / delta, (hi - origin) / delta))
            lower, upper = max(lower, a), min(upper, b)
            if lower > upper:
                return None
        return lower, upper


class FormPageAggregator(PDFPageAggregatorWithMarkedContent):
    def begin_figure(self, name: str, bbox: Rect, matrix: Matrix) -> None:
        super().begin_figure(name, bbox, matrix)
        figure = self.cur_item
        # A PDF /BBox has two corners. LTFigure's constructor expects width and height.
        # Its composed matrix already includes the page CTM and all enclosing transforms.
        figure.set_bbox(apply_matrix_rect(figure.matrix, bbox))
        figure.form_clip = Clip(bbox, figure.matrix)


class VisiblePage(Page):
    @property
    def layout(self):
        if not hasattr(self, "_layout"):
            device = FormPageAggregator(self.pdf.rsrcmgr, pageno=self.page_number, laparams=self.pdf.laparams)
            PDFPageInterpreter(self.pdf.rsrcmgr, device).process_page(self.page_obj)
            self._layout = device.get_result()
        return self._layout

    def parse_objects(self):
        x0, top, x1, bottom = self.cropbox
        media_x, media_top = self.mediabox[:2]
        height = self.height + media_top
        crop = Clip((x0 - media_x, height - bottom, x1 - media_x, height - top), MATRIX_IDENTITY)
        objects = defaultdict(list)
        for item in self.visible_objects(self.layout, (crop,)):
            objects[item["object_type"]].append(item)
        return dict(objects)

    def visible_objects(self, items, clips):
        for item in items:
            if isinstance(item, LTFigure):
                yield from self.visible_objects(item, (*clips, item.form_clip))
            elif isinstance(item, LTContainer):
                yield from self.visible_objects(item, clips)
            elif isinstance(item, LTLine):
                line = self.visible_line(item, clips)
                if line is not None:
                    yield line
            elif isinstance(item, LTCurve):
                # A partly occluded rectangle is not a reliable source of table rulings.
                if all(clip.contains((x, y)) for clip in clips
                       for x in (item.x0, item.x1) for y in (item.y0, item.y1)):
                    yield self.process_object(item)
            else:
                centre = ((item.x0 + item.x1) / 2, (item.y0 + item.y1) / 2)
                if all(clip.contains(centre) for clip in clips):
                    yield self.process_object(item)

    def visible_line(self, line, clips):
        start, stop = line.pts
        lower, upper = 0., 1.
        for clip in clips:
            limits = clip.segment(start, stop)
            if limits is None:
                return None
            lower, upper = max(lower, limits[0]), min(upper, limits[1])
        if lower >= upper:
            return None
        item = self.process_object(line)
        if lower > 0 or upper < 1:
            points = [self.point2coord(tuple(a + t * (b - a) for a, b in zip(start, stop)))
                      for t in (lower, upper)]
            left, right = sorted(p[0] for p in points)
            top, bottom = sorted(p[1] for p in points)
            height = self.height + self.mediabox[1]
            item.update(x0=left, x1=right, top=top, bottom=bottom, doctop=self.initial_doctop + top,
                        y0=height - bottom, y1=height - top, width=right - left, height=bottom - top,
                        pts=points, path=[("m", points[0]), ("l", points[1])])
        return item


def visible_page(page):
    return VisiblePage(page.pdf, page.page_obj, page.page_number, page.initial_doctop)
