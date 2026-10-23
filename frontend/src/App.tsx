import { parserOptions } from "./parserOptions";

export function App() {
  return (
    <main>
      <header>
        <p>PDF extraction and summarization</p>
        <h1>Async document processor</h1>
      </header>

      <form>
        <fieldset>
          <legend>Choose how to read the PDFs</legend>
          {parserOptions.map((option, index) => (
            <label key={option.value} htmlFor={`parser-${option.value}`}>
              <input
                defaultChecked={index === 0}
                id={`parser-${option.value}`}
                name="parser"
                type="radio"
                value={option.value}
              />
              <strong>{option.label}</strong>
              <span>{option.description}</span>
            </label>
          ))}
        </fieldset>
        <p>
          Every completed document uses Gemini for its summary, including files extracted locally
          with PyPDF or parsed with Mistral OCR.
        </p>
      </form>
    </main>
  );
}
