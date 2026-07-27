import pandas as pd
from typing import Iterable, Any, List

class RumorForm:

    form = None
    release = None
    source = None

    def __init__(self, rumor_class: str):
        self.rumor_class = rumor_class

    def add_form_attribute(self, attribute: Any) -> None:
        if attribute.type == 'source':
            self.source = attribute
        if attribute.type == 'form':
            self.form = attribute
        if attribute.type == 'release':
            self.release = attribute

    def validate(self):
        errors = []
        if self.form is None:
            errors.append('Missing a rumor form')
        if self.release is None:
            errors.append('Missing a release type')
        if self.source is None:
            errors.append('Missing a source')
        if not len(errors):
            errors += self.release.validate(form=self.form, source=self.source)
            errors += self.source.validate(form=self.form, release=self.release)
            errors += self.form.validate(form=self.source, release=self.release)
        return errors

class Release:

    type = 'release'

    def __init__(self, form_row: Iterable[tuple[Any, ...]]) -> None:
        self.id = form_row['release_type_id']
        self.name = form_row['release_type_name']
        self.level = form_row['release_type_level']
        self.valid_forms = [int(f) for f in form_row['valid_forms'].split(',')]

    def validate(self, form = None, source = None) -> List[str]:
        errors = []
        if form and form.id not in self.valid_forms:
            errors.append('Invalid form for this release type')
        if source and source.level > self.level:
            errors.append('Invalid source for this release type')
        return errors

class Source:

    type = 'source'

    def __init__(self, form_row: Iterable[tuple[Any, ...]]) -> None:
        self.id = form_row['source_type_id']
        self.name = form_row['source_type_name']
        self.level = form_row['source_type_level']
        self.valid_forms = [int(f) for f in form_row['valid_forms'].split(',')]

    def validate(self, form = None, release = None) -> List[str]:
        errors = []
        if form and form.id not in self.valid_forms:
            errors.append('Invalid form for this release type')
        if release and release.level < self.level:
            errors.append('Invalid source for this release type')
        return errors

class Form:

    type = 'form'

    def __init__(self, form_row: Iterable[tuple[Any, ...]]) -> None:
        self.id = form_row['rumor_form_id']
        self.name = form_row['release_type_name']
        self.level = form_row['release_type_level']
        self.valid_forms = [int(f) for f in form_row['valid_forms'].split(',')]

    def validate(self, form = None, source = None) -> List[str]:
        errors = []
        if form and form.id not in self.valid_forms:
            errors.append('Invalid form for this release type')
        if source and source.level > self.level:
            errors.append('Invalid source for this release type')
        return errors