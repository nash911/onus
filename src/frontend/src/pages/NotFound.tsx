import { Link } from "react-router-dom";
import { PageTitle } from "../components/States";

export default function NotFound() {
  return (
    <>
      <PageTitle>Page not found</PageTitle>
      <p>
        <Link className="text-blue-800 underline" to="/">
          Back to the start page
        </Link>
      </p>
    </>
  );
}
