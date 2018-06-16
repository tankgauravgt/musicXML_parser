# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""MusicXML parser.

Simple MusicXML parser used to convert MusicXML
into tensorflow.magenta.NoteSequence.
"""

# Imports
# Python 2 uses integer division for integers. Using this gives the Python 3
# behavior of producing a float when dividing integers
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from fractions import Fraction
import xml.etree.ElementTree as ET
import zipfile
from magenta.exception import MusicXMLParseException, MultipleTimeSignatureException

# internal imports

import six
import magenta.constants

from magenta.measure import Measure
from magenta.tempo import Tempo
from magenta.key_signature import KeySignature
from magenta.score_part import ScorePart
from magenta.part import Part

DEFAULT_MIDI_PROGRAM = 0    # Default MIDI Program (0 = grand piano)
DEFAULT_MIDI_CHANNEL = 0    # Default MIDI Channel (0 = first channel)
MUSICXML_MIME_TYPE = 'application/vnd.recordare.musicxml+xml'

class MusicXMLParserState(object):
  """Maintains internal state of the MusicXML parser."""

  def __init__(self):
    # Default to one division per measure
    # From the MusicXML documentation: "The divisions element indicates
    # how many divisions per quarter note are used to indicate a note's
    # duration. For example, if duration = 1 and divisions = 2,
    # this is an eighth note duration."
    self.divisions = 1

    # Default to a tempo of 120 quarter notes per minute
    # MusicXML calls this tempo, but Magenta calls this qpm
    # Therefore, the variable is called qpm, but reads the
    # MusicXML tempo attribute
    # (120 qpm is the default tempo according to the
    # Standard MIDI Files 1.0 Specification)
    self.qpm = 120

    # Duration of a single quarter note in seconds
    self.seconds_per_quarter = 0.5

    # Running total of time for the current event in seconds.
    # Resets to 0 on every part. Affected by <forward> and <backup> elements
    self.time_position = 0
    self.xml_position = 0

    # Default to a MIDI velocity of 64 (mf)
    self.velocity = 64

    # Default MIDI program (0 = grand piano)
    self.midi_program = DEFAULT_MIDI_PROGRAM

    # Current MIDI channel (usually equal to the part number)
    self.midi_channel = DEFAULT_MIDI_CHANNEL

    # Keep track of previous note to get chord timing correct
    # This variable stores an instance of the Note class (defined below)
    self.previous_note = None

    # Keep track of current transposition level in +/- semitones.
    self.transpose = 0

    # Keep track of current time signature. Does not support polymeter.
    self.time_signature = None


class MusicXMLDocument(object):
  """Internal representation of a MusicXML Document.

  Represents the top level object which holds the MusicXML document
  Responsible for loading the .xml or .mxl file using the _get_score method
  If the file is .mxl, this class uncompresses it

  After the file is loaded, this class then parses the document into memory
  using the parse method.
  """

  def __init__(self, filename):
    self._score = self._get_score(filename)
    self.parts = []
    # ScoreParts indexed by id.
    self._score_parts = {}
    self.midi_resolution = magenta.constants.STANDARD_PPQ
    self._state = MusicXMLParserState()
    # Total time in seconds
    self.total_time_secs = 0
    self.total_time_duration = 0
    self._parse()

  @staticmethod
  def _get_score(filename):
    """Given a MusicXML file, return the score as an xml.etree.ElementTree.

    Given a MusicXML file, return the score as an xml.etree.ElementTree
    If the file is compress (ends in .mxl), uncompress it first

    Args:
        filename: The path of a MusicXML file

    Returns:
      The score as an xml.etree.ElementTree.

    Raises:
      MusicXMLParseException: if the file cannot be parsed.
    """
    score = None
    if filename.endswith('.mxl'):
      # Compressed MXL file. Uncompress in memory.
      try:
        mxlzip = zipfile.ZipFile(filename)
      except zipfile.BadZipfile as exception:
        raise MusicXMLParseException(exception)

      # A compressed MXL file may contain multiple files, but only one
      # MusicXML file. Read the META-INF/container.xml file inside of the
      # MXL file to locate the MusicXML file within the MXL file
      # http://www.musicxml.com/tutorial/compressed-mxl-files/zip-archive-structure/

      # Raise a MusicXMLParseException if multiple MusicXML files found

      infolist = mxlzip.infolist()
      if six.PY3:
        # In py3, instead of returning raw bytes, ZipFile.infolist() tries to
        # guess the filenames' encoding based on file headers, and decodes using
        # this encoding in order to return a list of strings. If the utf-8
        # header is missing, it decodes using the DOS code page 437 encoding
        # which is almost definitely wrong. Here we need to explicitly check
        # for when this has occurred and change the encoding to utf-8.
        # https://stackoverflow.com/questions/37723505/namelist-from-zipfile-returns-strings-with-an-invalid-encoding
        zip_filename_utf8_flag = 0x800
        for info in infolist:
          if info.flag_bits & zip_filename_utf8_flag == 0:
            filename_bytes = info.filename.encode('437')
            filename = filename_bytes.decode('utf-8', 'replace')
            info.filename = filename

      container_file = [x for x in infolist
                        if x.filename == 'META-INF/container.xml']
      compressed_file_name = ''

      if container_file:
        try:
          container = ET.fromstring(mxlzip.read(container_file[0]))
          for rootfile_tag in container.findall('./rootfiles/rootfile'):
            if 'media-type' in rootfile_tag.attrib:
              if rootfile_tag.attrib['media-type'] == MUSICXML_MIME_TYPE:
                if not compressed_file_name:
                  compressed_file_name = rootfile_tag.attrib['full-path']
                else:
                  raise MusicXMLParseException(
                      'Multiple MusicXML files found in compressed archive')
            else:
              # No media-type attribute, so assume this is the MusicXML file
              if not compressed_file_name:
                compressed_file_name = rootfile_tag.attrib['full-path']
              else:
                raise MusicXMLParseException(
                    'Multiple MusicXML files found in compressed archive')
        except ET.ParseError as exception:
          raise MusicXMLParseException(exception)

      if not compressed_file_name:
        raise MusicXMLParseException(
            'Unable to locate main .xml file in compressed archive.')
      if six.PY2:
        # In py2, the filenames in infolist are utf-8 encoded, so
        # we encode the compressed_file_name as well in order to
        # be able to lookup compressed_file_info below.
        compressed_file_name = compressed_file_name.encode('utf-8')
      try:
        compressed_file_info = [x for x in infolist
                                if x.filename == compressed_file_name][0]
      except IndexError:
        raise MusicXMLParseException(
            'Score file %s not found in zip archive' % compressed_file_name)
      score_string = mxlzip.read(compressed_file_info)
      try:
        score = ET.fromstring(score_string)
      except ET.ParseError as exception:
        raise MusicXMLParseException(exception)
    else:
      # Uncompressed XML file.
      try:
        tree = ET.parse(filename)
        score = tree.getroot()
      except ET.ParseError as exception:
        raise MusicXMLParseException(exception)

    return score

  def _parse(self):
    """Parse the uncompressed MusicXML document."""
    # Parse part-list
    xml_part_list = self._score.find('part-list')
    if xml_part_list is not None:
      for element in xml_part_list:
        if element.tag == 'score-part':
          score_part = ScorePart(element)
          self._score_parts[score_part.id] = score_part

    # Parse parts
    for score_part_index, child in enumerate(self._score.findall('part')):
      part = Part(child, self._score_parts, self._state)
      self.parts.append(part)
      score_part_index += 1
      if self._state.time_position > self.total_time_secs:
        self.total_time_secs = self._state.time_position
      if self._state.xml_position > self.total_time_duration:
        self.total_time_duration = self._state.xml_position

  def get_chord_symbols(self):
    """Return a list of all the chord symbols used in this score."""
    chord_symbols = []
    for part in self.parts:
      for measure in part.measures:
        for chord_symbol in measure.chord_symbols:
          if chord_symbol not in chord_symbols:
            # Prevent duplicate chord symbols
            chord_symbols.append(chord_symbol)
    return chord_symbols

  def get_time_signatures(self):
    """Return a list of all the time signatures used in this score.

    Does not support polymeter (i.e. assumes all parts have the same
    time signature, such as Part 1 having a time signature of 6/8
    while Part 2 has a simultaneous time signature of 2/4).

    Ignores duplicate time signatures to prevent Magenta duplicate
    time signature error. This happens when multiple parts have the
    same time signature is used in multiple parts at the same time.

    Example: If Part 1 has a time siganture of 4/4 and Part 2 also
    has a time signature of 4/4, then only instance of 4/4 is sent
    to Magenta.

    Returns:
      A list of all TimeSignature objects used in this score.
    """
    time_signatures = []
    for part in self.parts:
      for measure in part.measures:
        if measure.time_signature is not None:
          if measure.time_signature not in time_signatures:
            # Prevent duplicate time signatures
            time_signatures.append(measure.time_signature)

    return time_signatures

  def get_key_signatures(self):
    """Return a list of all the key signatures used in this score.

    Support different key signatures in different parts (score in
    written pitch).

    Ignores duplicate key signatures to prevent Magenta duplicate key
    signature error. This happens when multiple parts have the same
    key signature at the same time.

    Example: If the score is in written pitch and the
    flute is written in the key of Bb major, the trombone will also be
    written in the key of Bb major. However, the clarinet and trumpet
    will be written in the key of C major because they are Bb transposing
    instruments.

    If no key signatures are found, create a default key signature of
    C major.

    Returns:
      A list of all KeySignature objects used in this score.
    """
    key_signatures = []
    for part in self.parts:
      for measure in part.measures:
        if measure.key_signature is not None:
          if measure.key_signature not in key_signatures:
            # Prevent duplicate key signatures
            key_signatures.append(measure.key_signature)

    if not key_signatures:
      # If there are no key signatures, add C major at the beginning
      key_signature = KeySignature(self._state)
      key_signature.time_position = 0
      key_signature.xml_position = 0
      key_signatures.append(key_signature)

    return key_signatures

  def get_tempos(self):
    """Return a list of all tempos in this score.

    If no tempos are found, create a default tempo of 120 qpm.

    Returns:
      A list of all Tempo objects used in this score.
    """
    tempos = []

    if self.parts:
      part = self.parts[0]  # Use only first part
      for measure in part.measures:
        for tempo in measure.tempos:
          tempos.append(tempo)

    # If no tempos, add a default of 120 at beginning
    if not tempos:
      tempo = Tempo(self._state)
      tempo.qpm = self._state.qpm
      tempo.time_position = 0
      tempo.xml_position = 0
      tempos.append(tempo)

    return tempos

  def recalculate_time_position(self):
    tempos = self.get_tempos()

    tempos.sort(key=lambda x: x.xml_position)
    new_time_position =0
    for i in range(len(tempos)):
      tempos[i].time_position = new_time_position
      if i +1 < len(tempos):
        new_time_position +=  (tempos[i+1].xml_position - tempos[i].xml_position) / tempos[i].qpm * 60 / tempos[i].state.divisions

    for part in self.parts:
      for measure in part.measures:
        print(measure.start_xml_position)
        for note in measure.notes:
          for i in range(len(tempos)):
            if i + 1 == len(tempos):
              current_tempo = tempos[i].qpm * 60 / tempos[i].state.divisions
              break
            else:
              if tempos[i].xml_position <= note.note_duration.xml_position and tempos[i+1].xml_position > note.note_duration.xml_position:
                current_tempo = tempos[i].qpm * 60 / tempos[i].state.divisions
                break
          note.note_duration.time_position = tempos[i].time_position + (note.note_duration.xml_position - tempos[i].xml_position) / current_tempo




