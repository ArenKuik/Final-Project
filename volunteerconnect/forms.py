"""Flask-WTF forms. Every FlaskForm includes a hidden CSRF token automatically."""
from flask_wtf import FlaskForm
from wtforms import (DateField, EmailField, FieldList, Form, FormField, IntegerField,
                     PasswordField, RadioField, SelectField, SelectMultipleField, StringField,
                     TextAreaField, TimeField, widgets)
from wtforms.validators import (DataRequired, InputRequired, Length, NumberRange, Optional,
                                Regexp, ValidationError)

# A simple shape check (something@something.something). Good enough for a
# login name; we never send email, so we don't need stricter validation.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
MIN_PASSWORD_LENGTH = 10

# The clearance types the app knows about (stored values, display labels).
CLEARANCE_TYPES = [
    ("tb_test", "TB test"),
    ("background_check", "Background check"),
]


class RegisterForm(FlaskForm):
    email = EmailField("Email", validators=[
        DataRequired(),
        Length(max=254),
        Regexp(EMAIL_PATTERN, message="Enter a valid email address."),
    ])
    password = PasswordField("Password", validators=[
        DataRequired(),
        Length(min=MIN_PASSWORD_LENGTH, max=200,
               message=f"Password must be at least {MIN_PASSWORD_LENGTH} characters."),
    ])
    # Only these two choices are accepted. A hand-built POST with
    # role=coordinator fails validation ("Not a valid choice").
    role = RadioField("I am a", choices=[
        ("volunteer", "Volunteer"),
        ("teacher", "Teacher"),
    ], validators=[DataRequired()])
    invite_code = StringField("School invite code", validators=[Length(max=64)])

    def validate_invite_code(self, field):
        """Teachers must enter a code. Whether it matches a school is checked in the route."""
        if self.role.data == "teacher" and not (field.data or "").strip():
            raise ValidationError("Teachers need the invite code from their school.")


class LoginForm(FlaskForm):
    email = EmailField("Email", validators=[DataRequired(), Length(max=254)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=200)])


class ActionForm(FlaskForm):
    """No fields: used for one-click POST buttons (revoke, regenerate...)
    so that they still send a CSRF token."""


class LogoutForm(ActionForm):
    """The navbar's logout button."""


class MultiCheckboxField(SelectMultipleField):
    """A list of checkboxes; only values from `choices` are accepted."""
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()


class SchoolForm(FlaskForm):
    name = StringField("School name", validators=[DataRequired(), Length(max=120)])
    address = StringField("Address", validators=[Optional(), Length(max=200)])
    required_clearances = MultiCheckboxField("Required clearances", choices=CLEARANCE_TYPES)


class ClearanceForm(FlaskForm):
    type = SelectField("Clearance type", choices=CLEARANCE_TYPES, validators=[DataRequired()])
    # The "not in the past" check needs today's date in Los Angeles, which
    # the route gets from the database (see clearances.py).
    expires_on = DateField("Expires on", validators=[DataRequired(message="Enter an expiry date.")])


# ---------- help requests and shifts ----------
# "Date not in the past" needs today's date in Los Angeles, which comes from
# the database, so the routes check it (see help_requests.py).

MAX_VOLUNTEERS_PER_SHIFT = 50
MAX_SHIFTS_PER_REQUEST = 6


def end_after_start(form, field):
    """Shared validator: end_time must be later than start_time."""
    start = form.start_time.data
    if start and field.data and field.data <= start:
        raise ValidationError("End time must be after the start time.")


class HelpRequestForm(FlaskForm):
    activity = StringField("Activity", validators=[DataRequired(), Length(max=120)])
    room = StringField("Room", validators=[Optional(), Length(max=60)])
    date = DateField("Date", validators=[DataRequired(message="Enter a date.")])
    start_time = TimeField("Start time", validators=[DataRequired(message="Enter a start time.")])
    end_time = TimeField("End time", validators=[DataRequired(message="Enter an end time."), end_after_start])
    # InputRequired (not DataRequired) so that 0 gets the "at least 1" message.
    volunteers_needed = IntegerField("Volunteers needed", validators=[
        InputRequired(), NumberRange(min=1, max=MAX_VOLUNTEERS_PER_SHIFT,
                                     message=f"Enter a number from 1 to {MAX_VOLUNTEERS_PER_SHIFT}."),
    ])


class ShiftRowForm(Form):
    """One shift inside the approve form. A plain Form (not FlaskForm)
    because the outer ApproveForm already carries the CSRF token."""
    date = DateField("Date", validators=[DataRequired(message="Enter a date.")])
    start_time = TimeField("Start", validators=[DataRequired(message="Enter a start time.")])
    end_time = TimeField("End", validators=[DataRequired(message="Enter an end time."), end_after_start])
    capacity = IntegerField("Volunteers", validators=[
        InputRequired(), NumberRange(min=1, max=MAX_VOLUNTEERS_PER_SHIFT,
                                     message=f"Enter a number from 1 to {MAX_VOLUNTEERS_PER_SHIFT}."),
    ])


class ApproveForm(FlaskForm):
    shifts = FieldList(FormField(ShiftRowForm), min_entries=1, max_entries=MAX_SHIFTS_PER_REQUEST)


class SendBackForm(FlaskForm):
    note = TextAreaField("What needs to change?", validators=[
        DataRequired(message="Write a note so the teacher knows what to change."), Length(max=1000),
    ])


class RejectForm(FlaskForm):
    reject_note = TextAreaField("Reason (optional)", validators=[Optional(), Length(max=1000)])
