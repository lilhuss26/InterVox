from marshmallow import Schema, fields, validate

class InterviewStartRequest(Schema):
    job_description = fields.Str(required=True)

class StartInterviewResponse(Schema):
    session_id = fields.Str()
    first_question = fields.Str()

class ReportResponse(Schema):
    final_report = fields.Dict()
    coaching_notes = fields.Dict()

class ModelSelectionRequest(Schema):
    model = fields.Str(required=True)

class ModelSelectionResponse(Schema):
    model = fields.Str()
    available_models = fields.List(fields.Str())
