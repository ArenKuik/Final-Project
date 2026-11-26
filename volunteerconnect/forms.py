"""Flask-WTF forms. Every FlaskForm includes a hidden CSRF token automatically."""
from flask_wtf import FlaskForm
from wtforms import EmailField, PasswordField, RadioField, StringField
from wtforms.validators import DataRequired, Length, Regexp, ValidationError

# A simple shape check (something@something.something). Good enough for a
# login name; we never send email, so we don't need stricter validation.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
MIN_PASSWORD_LENGTH = 10


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


class LogoutForm(FlaskForm):
    """No fields: it exists so the logout button sends a CSRF token."""
