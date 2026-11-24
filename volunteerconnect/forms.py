"""Flask-WTF forms. Every FlaskForm includes a hidden CSRF token automatically."""
from flask_wtf import FlaskForm
from wtforms import (DateField, EmailField, PasswordField, RadioField, SelectField,
                     SelectMultipleField, StringField, widgets)
from wtforms.validators import DataRequired, Length, Optional, Regexp, ValidationError

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
