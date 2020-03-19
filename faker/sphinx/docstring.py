# coding=utf-8
import re

from collections import namedtuple

from faker import Faker
from faker.config import AVAILABLE_LOCALES, DEFAULT_LOCALE
from faker.sphinx.validator import SampleCodeValidator
from sphinx.util import logging

logger = logging.getLogger(__name__)
_fake = Faker(AVAILABLE_LOCALES)
_base_provider_method_pattern = re.compile(r'^faker\.providers\.BaseProvider\.(?P<method>\w+)$')
_standard_provider_method_pattern = re.compile(r'^faker\.providers\.\w+\.Provider\.(?P<method>\w+)$')
_locale_provider_method_pattern = re.compile(
    r'^faker\.providers\.\w+'
    r'\.(?P<locale>[a-z]{2,3}_[A-Z]{2})'
    r'\.Provider'
    r'\.(?P<method>\w+)$',
)
_sample_line_pattern = re.compile(
    r'^:sample'
    r'(?: size=(?P<size>[1-9][0-9]*))?'
    r'(?: seed=(?P<seed>[0-9]+))?'
    r':'
    r'(?: ?(?P<kwargs>.*))?$',
)
_command_template = 'generator.{method}({kwargs})'
_sample_output_template = (
    '>>> Faker.seed({seed})\n'
    '>>> for _ in range({size}):\n'
    '...     fake.{method}({kwargs})\n'
    '...\n'
    '{results}\n'
)

DEFAULT_SAMPLE_SIZE = 5
DEFAULT_SEED = 0
Sample = namedtuple('Sample', ['size', 'seed', 'kwargs'])


class ProviderMethodDocstring:
    """
    Class that preprocesses provider method docstrings to generate sample usage and output

    Notes on how samples are generated:
    - If the docstring belongs to a standard provider method, sample usage and output will be
      generated using a `Faker` object in the `DEFAULT_LOCALE`.
    - If the docstring belongs to a localized provider method, the correct locale will be used.
    - If the docstring does not belong to any provider method, docstring preprocessing will be skipped.
    - Docstring lines will be parsed for potential sample sections, and the generation details of each
      sample section will internally be represented as a ``Sample`` namedtuple.
    - Each ``Sample`` will have info on the keyword arguments to pass to the provider method, how many
      times the provider method will be called, and the initial seed value to ``Faker.seed()``.
    """

    def __init__(self, app, what, name, obj, options, lines):
        self._line_iter = iter(lines)
        self._parsed_lines = []
        self._samples = []
        self._skipped = True

        if what != 'method':
            return

        base_provider_method_match = _base_provider_method_pattern.match(name)
        locale_provider_method_match = _locale_provider_method_pattern.match(name)
        standard_provider_method_match = _standard_provider_method_pattern.match(name)
        if base_provider_method_match:
            groupdict = base_provider_method_match.groupdict()
            self._method = groupdict['method']
            self._locale = DEFAULT_LOCALE
        elif standard_provider_method_match:
            groupdict = standard_provider_method_match.groupdict()
            self._method = groupdict['method']
            self._locale = DEFAULT_LOCALE
        elif locale_provider_method_match:
            groupdict = locale_provider_method_match.groupdict()
            self._method = groupdict['method']
            self._locale = groupdict['locale']
        else:
            return

        self._skipped = False
        self._parse()
        self._generate_samples()

    def _parse(self):
        while True:
            try:
                line = next(self._line_iter)
            except StopIteration:
                break
            else:
                self._parse_section(line)

    def _parse_section(self, section):
        # No-op if section does not look like the start of a sample section
        if not section.startswith(':sample'):
            self._parsed_lines.append(section)
            return

        try:
            next_line = next(self._line_iter)
        except StopIteration:
            # No more lines left to consume, so save current sample section
            self._process_sample_section(section)
            return

        # Next line is the start of a new sample section, so process
        # current sample section, and start parsing the new section
        if next_line.startswith(':sample'):
            self._process_sample_section(section)
            self._parse_section(next_line)

        # Next line is an empty line indicating the end of
        # current sample section, so process current section
        elif next_line == '':
            self._process_sample_section(section)

        # Section is assumed to be multiline, so continue
        # adding lines to current sample section
        else:
            section = section + next_line
            self._parse_section(section)

    def _process_sample_section(self, section):
        match = _sample_line_pattern.match(section)

        # Discard sample section if malformed
        if not match:
            return

        # Set sample generation defaults and do some beautification if necessary
        groupdict = match.groupdict()
        size = groupdict.get('size')
        seed = groupdict.get('seed')
        kwargs = groupdict.get('kwargs')
        size = max(int(size), DEFAULT_SAMPLE_SIZE) if size else DEFAULT_SAMPLE_SIZE
        seed = int(seed) if seed else DEFAULT_SEED
        kwargs = self._beautify_kwargs(kwargs) if kwargs else ''

        # Store sample generation details
        sample = Sample(size, seed, kwargs)
        self._samples.append(sample)

    def _beautify_kwargs(self, kwargs):
        def _repl_whitespace(match):
            quoted = match.group(1) or match.group(2)
            return quoted if quoted else ''

        def _repl_comma(match):
            quoted = match.group(1) or match.group(2)
            return quoted if quoted else ', '

        # First, remove all whitespaces and tabs not within quotes
        result = re.sub(r'("[^"]*")|(\'[^\']*\')|[ \t]+', _repl_whitespace, kwargs)

        # Next, insert a whitespace after each comma not within quotes
        result = re.sub(r'("[^"]*")|(\'[^\']*\')|,', _repl_comma, result)

        # Then return the result with all leading and trailing whitespaces stripped
        return result.strip()

    def _format_results(self, results):
        def _stringify_result(value):
            if value is None:
                return value
            elif value == '':
                return "''"
            else:
                return str(value)

        output = ''
        for result in results:
            output += '{}\n'.format(_stringify_result(result))
        return output

    def _generate_eval_scope(self):
        from collections import OrderedDict  # noqa: F401 Do not remove! The eval command needs this reference.
        return {
            'generator': _fake[self._locale],
            'OrderedDict': OrderedDict,
        }

    def _generate_samples(self):
        if not self._samples:
            return

        output = ''
        eval_scope = self._generate_eval_scope()
        for sample in self._samples:
            command = _command_template.format(method=self._method, kwargs=sample.kwargs)
            validator = SampleCodeValidator(command)
            if validator.errors:
                msg = (
                    'Invalid code elements detected. Sample generation will be '
                    'skipped for method `{method}` with arguments `{kwargs}`'
                ).format(
                    method=self._method, kwargs=sample.kwargs,
                )
                logger.warning(msg)
                continue

            try:
                Faker.seed(sample.seed)
                results = [eval(command, eval_scope) for _ in range(sample.size)]
            except Exception:
                msg = 'Sample generation failed for method `{method}` with arguments `{kwargs}`'.format(
                    method=self._method, kwargs=sample.kwargs,
                )
                logger.warning(msg)
                continue
            else:
                output += _sample_output_template.format(
                    seed=sample.seed, method=self._method, kwargs=sample.kwargs,
                    size=sample.size, results=self._format_results(results),
                )

        if output:
            output = ':examples:\n\n' + output
            self._parsed_lines.extend(output.split('\n'))

    @property
    def skipped(self):
        return self._skipped

    @property
    def lines(self):
        return self._parsed_lines
