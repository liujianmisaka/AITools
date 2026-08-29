export class TerminalHostError extends Error {
  readonly code: string;
  readonly statusCode: number;

  constructor(code: string, message: string, statusCode = 409) {
    super(message);
    this.name = "TerminalHostError";
    this.code = code;
    this.statusCode = statusCode;
  }
}
