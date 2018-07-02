from fractions import Fraction

import mxp.constants
from mxp.chord_symbol import ChordSymbol
from mxp.tempo import Tempo
from mxp.time_signature import TimeSignature
from mxp.key_signature import KeySignature
from mxp.exception import MultipleTimeSignatureException
from mxp.note import Note

class Measure(object):
  """Internal represention of the MusicXML <measure> element."""

  def __init__(self, xml_measure, state):
    self.xml_measure = xml_measure
    self.notes = []
    self.chord_symbols = []
    self.tempos = []
    self.time_signature = None
    self.key_signature = None
    self.barline = None            # 'double' or 'final' or None
    self.repeat = None             # 'start' or 'stop' or None
    # Cumulative duration in MusicXML duration.
    # Used for time signature calculations
    self.duration = 0
    self.state = state
    # Record the starting time of this measure so that time signatures
    # can be inserted at the beginning of the measure
    self.start_time_position = self.state.time_position
    self.start_xml_position = self.state.xml_position
    self._parse()
    # Update the time signature if a partial or pickup measure
    self._fix_time_signature()

  def _parse(self):
    """Parse the <measure> element."""
    # Create new direction
    direction = []
    for child in self.xml_measure:
      if child.tag == 'attributes':
        self._parse_attributes(child)
      elif child.tag == 'backup':
        self._parse_backup(child)
      elif child.tag == 'barline':
        self._parse_barline(child)
      elif child.tag == 'direction':
        # Append new direction
        direction.append(child)
        # Get tempo in <sound /> and update state tempo and time_position
        self._parse_direction(child)
      elif child.tag == 'forward':
        self._parse_forward(child)
      elif child.tag == 'harmony':
        chord_symbol = ChordSymbol(child, self.state)
        self.chord_symbols.append(chord_symbol)
      elif child.tag == 'note':
        # Add direction if find note 
        note = Note(child, direction, self.state)
        self.notes.append(note)
        # Keep track of current note as previous note for chord timings
        self.state.previous_note = note
        # Make empty direction
        direction = []
        # Sum up the MusicXML durations in voice 1 of this measure
        if note.voice == 1 and not note.is_in_chord:
          self.duration += note.note_duration.duration
      else:
        # Ignore other tag types because they are not relevant.
        pass

  def _parse_barline(self, xml_barline):
    """Parse the MusicXML <barline> element.

    Args:
      xml_barline: XML element with tag type 'barline'.
    """
    style = xml_barline.find('bar-style').text
    repeat = xml_barline.find('repeat')

    if style == 'light-light':
      self.barline = 'double'
    elif style == 'light-heavy':
      self.barline = 'final'
    elif repeat is not None:
      attrib = repeat.attrib['direction']
      if attrib == 'forward':
        self.repeat = 'start'
      elif attrib == 'backword':
        self.repeat = 'end'
    
  def _parse_attributes(self, xml_attributes):
    """Parse the MusicXML <attributes> element."""

    for child in xml_attributes:
      if child.tag == 'divisions':
        self.state.divisions = int(child.text)
      elif child.tag == 'key':
        self.key_signature = KeySignature(self.state, child)
      elif child.tag == 'time':
        if self.time_signature is None:
          self.time_signature = TimeSignature(self.state, child)
          self.state.time_signature = self.time_signature
        else:
          raise MultipleTimeSignatureException('Multiple time signatures')
      elif child.tag == 'transpose':
        transpose = int(child.find('chromatic').text)
        self.state.transpose = transpose
        if self.key_signature is not None:
          # Transposition is chromatic. Every half step up is 5 steps backward
          # on the circle of fifths, which has 12 positions.
          key_transpose = (transpose * -5) % 12
          new_key = self.key_signature.key + key_transpose
          # If the new key has >6 sharps, translate to flats.
          # TODO(fjord): Could be more smart about when to use sharps vs. flats
          # when there are enharmonic equivalents.
          if new_key > 6:
            new_key %= -6
          self.key_signature.key = new_key

        
      else:
        # Ignore other tag types because they are not relevant to mxp.
        pass

  def _parse_backup(self, xml_backup):
    """Parse the MusicXML <backup> element.

    This moves the global time position backwards.

    Args:
      xml_backup: XML element with tag type 'backup'.
    """

    xml_duration = xml_backup.find('duration')
    backup_duration = int(xml_duration.text)
    midi_ticks = backup_duration * (mxp.constants.STANDARD_PPQ
                                    / self.state.divisions)
    seconds = ((midi_ticks / mxp.constants.STANDARD_PPQ)
               * self.state.seconds_per_quarter)
    self.state.time_position -= seconds
    self.state.xml_position -= backup_duration

  def _parse_direction(self, xml_direction):
    """Parse the MusicXML <direction> element."""
    for child in xml_direction:
      if child.tag == 'sound':
        if child.get('tempo') is not None:
          tempo = Tempo(self.state, child)
          self.tempos.append(tempo)
          self.state.qpm = tempo.qpm
          self.state.seconds_per_quarter = 60 / self.state.qpm
          if child.get('dynamics') is not None:
            self.state.velocity = int(child.get('dynamics'))

  def _parse_forward(self, xml_forward):
    """Parse the MusicXML <forward> element.

    This moves the global time position forward.

    Args:
      xml_forward: XML element with tag type 'forward'.
    """

    xml_duration = xml_forward.find('duration')
    forward_duration = int(xml_duration.text)
    midi_ticks = forward_duration * (mxp.constants.STANDARD_PPQ
                                     / self.state.divisions)
    seconds = ((midi_ticks / mxp.constants.STANDARD_PPQ)
               * self.state.seconds_per_quarter)
    self.state.time_position += seconds
    self.state.xml_position += forward_duration

  def _fix_time_signature(self):
    """Correct the time signature for incomplete measures.

    If the measure is incomplete or a pickup, insert an appropriate
    time signature into this Measure.
    """
    # Compute the fractional time signature (duration / divisions)
    # Multiply divisions by 4 because division is always parts per quarter note
    numerator = self.duration
    denominator = self.state.divisions * 4
    fractional_time_signature = Fraction(numerator, denominator)

    if self.state.time_signature is None and self.time_signature is None:
      # No global time signature yet and no measure time signature defined
      # in this measure (no time signature or senza misura).
      # Insert the fractional time signature as the time signature
      # for this measure
      self.time_signature = TimeSignature(self.state)
      self.time_signature.numerator = fractional_time_signature.numerator
      self.time_signature.denominator = fractional_time_signature.denominator
      self.state.time_signature = self.time_signature
    else:
      fractional_state_time_signature = Fraction(
          self.state.time_signature.numerator,
          self.state.time_signature.denominator)

      # Check for pickup measure. Reset time signature to smaller numerator
      pickup_measure = False
      if numerator < self.state.time_signature.numerator:
        pickup_measure = True

      # Get the current time signature denominator
      global_time_signature_denominator = self.state.time_signature.denominator

      # If the fractional time signature = 1 (e.g. 4/4),
      # make the numerator the same as the global denominator
      if fractional_time_signature == 1 and not pickup_measure:
        new_time_signature = TimeSignature(self.state)
        new_time_signature.numerator = global_time_signature_denominator
        new_time_signature.denominator = global_time_signature_denominator
      else:
        # Otherwise, set the time signature to the fractional time signature
        # Issue #674 - Use the original numerator and denominator
        # instead of the fractional one
        new_time_signature = TimeSignature(self.state)
        new_time_signature.numerator = numerator
        new_time_signature.denominator = denominator

        new_time_sig_fraction = Fraction(numerator,
                                         denominator)

        if new_time_sig_fraction == fractional_time_signature:
          new_time_signature.numerator = fractional_time_signature.numerator
          new_time_signature.denominator = fractional_time_signature.denominator

      # Insert a new time signature only if it does not equal the global
      # time signature.
      if (pickup_measure or
          (self.time_signature is None
           and (fractional_time_signature != fractional_state_time_signature))):
        new_time_signature.time_position = self.start_time_position
        new_time_signature.xml_position = self.start_xml_position
        self.time_signature = new_time_signature
        self.state.time_signature = new_time_signature